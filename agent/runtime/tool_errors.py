"""Return field-level argument errors without echoing submitted private values."""

import json

from tools.result import invalid_args


def validation_result(error):
    fields = [{"field": ".".join(str(part) for part in item["loc"]),
               "error": item["type"], "message": item["msg"]}
              for item in error.errors(include_input=False, include_url=False)[:8]]
    return invalid_args(json.dumps({"error": "invalid_arguments", "fields": fields},
                                   ensure_ascii=False))
