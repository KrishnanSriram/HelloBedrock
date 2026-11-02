"""
Lambda entry point: validate the request, apply the project's config, run the shared
guardrail flow, and return the result.

    project_config.py  where each project's prompt and token limit come from
    guardrails.py      precheck, model call, and guardrail results (shared by all projects)
    responses.py       HTTP response formatting
"""
import json

import guardrails
from project_config import get_project, ConfigUnavailable
from responses import respond, error

MAX_PROMPT_CHARS = 8000
NO_USAGE = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}


def lambda_handler(event, context):
    # 1. Validate the request.
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return error(400, "Body must be valid JSON")
    if not isinstance(body, dict):
        return error(400, "Body must be a JSON object")

    # 2. Look up the project. Any system prompt sent by the caller is ignored.
    project_id = body.get("project")
    try:
        project = get_project(project_id)
    except ConfigUnavailable:
        return error(503, "Project configuration unavailable, please retry")
    if project is None:
        return error(400, "Missing or unknown 'project'")

    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return error(400, "Missing 'prompt'")
    if len(prompt) > MAX_PROMPT_CHARS:
        return error(413, f"Prompt exceeds {MAX_PROMPT_CHARS} characters")
    try:
        max_tokens = min(max(int(body.get("max_tokens", project.max_tokens)), 1), project.max_tokens)
    except (TypeError, ValueError):
        return error(400, "'max_tokens' must be an integer")

    try:
        # 3. Mask PII/PCI before the model sees anything; refuse if something must be blocked.
        masked_prompt, pre_hits, pre_blocked = guardrails.precheck(prompt)
        if pre_blocked:
            hits = sorted(pre_hits)
            print(json.dumps({"project": project_id, "blocked": True,
                              "stop_reason": "precheck_blocked", "guardrail_hits": hits}))
            return respond(422, {"project": project_id, "blocked": True,
                                 "stop_reason": "precheck_blocked", "guardrail_hits": hits,
                                 "response": guardrails.BLOCK_MESSAGE, "usage": NO_USAGE})

        # 4. Call the model with this project's prompt and the masked user text.
        r = guardrails.invoke_model(project.system_prompt, masked_prompt, max_tokens)
    except guardrails.ThrottlingException:
        return error(429, "Throttled, please retry")
    except Exception as e:
        print(f"Bedrock error: {type(e).__name__}: {str(e)[:300]}")
        return error(502, "Model invocation failed")

    # 5. Decide what to return, log it (never the prompt), and respond.
    text, stop, model_hits, blocked = guardrails.evaluate(r)
    hits = sorted(pre_hits | model_hits)
    print(json.dumps({"project": project_id, "blocked": blocked, "stop_reason": stop,
                      "guardrail_hits": hits, "usage": r.get("usage")}))
    return respond(422 if blocked else 200,
                   {"project": project_id, "blocked": blocked, "stop_reason": stop,
                    "guardrail_hits": hits, "response": text, "usage": r.get("usage")})