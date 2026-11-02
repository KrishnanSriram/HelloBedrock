"""
Everything that talks to Bedrock: the guardrail precheck, the model call, and reading
guardrail results. Shared by every project; handler.py only orchestrates.

Environment variables: MODEL_ID, GUARDRAIL_ID, GUARDRAIL_VERSION
"""
import os
import boto3

brt = boto3.client("bedrock-runtime")
MODEL_ID = os.environ["MODEL_ID"]
GUARDRAIL_ID = os.environ["GUARDRAIL_ID"]
GUARDRAIL_VERSION = os.environ["GUARDRAIL_VERSION"]
GUARDRAIL = {
    "guardrailIdentifier": GUARDRAIL_ID,
    "guardrailVersion": GUARDRAIL_VERSION,
    "trace": "enabled",
}

# Re-exported so callers can catch throttling without touching boto3.
ThrottlingException = brt.exceptions.ThrottlingException

BLOCK_MESSAGE = "Request blocked by our safety policy (sensitive data or disallowed content)."

# The guardrail masks these entities. A match with at least this many digits is a
# full number, so the request is refused. Fewer digits (e.g. last 4) stay masked and allowed.
FULL_NUMBER_DIGITS = {
    "CREDIT_DEBIT_CARD_NUMBER": 13,
    "US_SOCIAL_SECURITY_NUMBER": 9,
}


def hits_from(assessments):
    """Which guardrail policies fired (types only, never the matched text)."""
    hits = set()
    for a in assessments:
        for f in a.get("contentPolicy", {}).get("filters", []):
            if f.get("action") == "BLOCKED":
                hits.add(f["type"])
        for p in a.get("sensitiveInformationPolicy", {}).get("piiEntities", []):
            action = p.get("action")
            if action not in ("BLOCKED", "ANONYMIZED"):
                continue
            # Full card number or SSN: refuse, even though the guardrail only masked it.
            # Only the digit count is used; the matched text is never logged or returned.
            min_digits = FULL_NUMBER_DIGITS.get(p.get("type"))
            if action == "ANONYMIZED" and min_digits:
                match = p.get("match")
                # Fail closed: if the match is missing, treat it as a full number.
                if match is None or sum(ch.isdigit() for ch in match) >= min_digits:
                    action = "BLOCKED"
            hits.add(f"{p['type']}:{action}")
    return hits


def precheck(prompt):
    """
    Mask sensitive data in the prompt BEFORE the model sees it.
    The guardrail only applies masking to OUTPUT, so the prompt is checked as OUTPUT here.
    Returns (masked_prompt, hits, blocked). Prompt-attack detection still happens in
    invoke_model() on INPUT.
    """
    r = brt.apply_guardrail(
        guardrailIdentifier=GUARDRAIL_ID,
        guardrailVersion=GUARDRAIL_VERSION,
        source="OUTPUT",
        content=[{"text": {"text": prompt}}],
    )
    hits = hits_from(r.get("assessments", []))
    masked = prompt
    if r.get("action") == "GUARDRAIL_INTERVENED":
        outputs = r.get("outputs") or []
        masked = "".join(o.get("text", "") for o in outputs)
        if not hits:               # fail closed: intervened but nothing readable
            hits.add("UNKNOWN:BLOCKED")
    # Block on any blocked entity, or any content filter (e.g. MISCONDUCT, which has no ":ACTION").
    blocked = any(h.endswith(":BLOCKED") or ":" not in h for h in hits)
    return masked, hits, blocked


def invoke_model(system_prompt, user_text, max_tokens):
    """
    Call the model with the guardrail attached. Only the user text is wrapped in
    guardContent, so the guardrail never scans the system prompt: a longer project
    prompt adds model tokens, not guardrail cost.
    """
    return brt.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[{"role": "user",
                   "content": [{"guardContent": {"text": {"text": user_text}}}]}],
        inferenceConfig={"maxTokens": max_tokens},
        guardrailConfig=GUARDRAIL,
    )


def converse_hits(r):
    """Guardrail hits from a converse() response, input and output side."""
    g = r.get("trace", {}).get("guardrail", {}) or {}
    assessments = list((g.get("inputAssessment") or {}).values())
    for v in (g.get("outputAssessments") or {}).values():
        assessments.extend(v if isinstance(v, list) else [v])
    return hits_from(assessments)


def evaluate(r):
    """
    Decide what to return for a converse() response.
    Returns (text, stop_reason, hits, blocked).
    """
    text = "".join(c.get("text", "") for c in r["output"]["message"]["content"])
    stop = r.get("stopReason")
    hits = converse_hits(r)

    # The guardrail reports "intervened" even when it only masked PII; that is not a block.
    masked_only = bool(hits) and all(h.endswith(":ANONYMIZED") for h in hits)
    # Fail closed: an intervention with no readable trace still counts as a block.
    # "content_filtered" is the model's own built-in filter (e.g. Nova), also a block.
    blocked = (stop == "guardrail_intervened" and not masked_only) or stop == "content_filtered"
    if any(h.split(":")[0] in FULL_NUMBER_DIGITS and h.endswith(":BLOCKED") for h in hits):
        text = BLOCK_MESSAGE   # model's reply contained a full number: withhold it
    return text, stop, hits, blocked