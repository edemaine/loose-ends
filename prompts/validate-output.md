# Required output validation

Write the exact structured result to `agent-result.json` in the current
workspace. It must match `result_schema` in `validation/expectations.json` and
pass the task-specific checks in `validation/validate.py`.

Before finishing, validate your output via the following command:

```text
python -m validation.validate
```

Fix every reported issue without modifying `validation/` or any staged input,
and rerun the command until it prints `Validation passed.` Do not change
generated output after the successful check. Your final assistant message is
only a short completion note; the driver reads `agent-result.json` directly.
