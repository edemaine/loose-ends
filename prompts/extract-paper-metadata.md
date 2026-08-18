# Extract paper metadata

Inspect `paper.pdf` and, when present, the submitted files under `source/`.
Return bibliographic metadata for the paper itself. Use the source to recover
exact names or machine-readable title data, but prefer the rendered PDF when
the sources disagree about what the paper displays.

- Copy the full displayed title faithfully, joining visual line breaks with spaces.
- List every displayed author in reading order, using the paper's own spelling and initials.
- Use ISO 8601 for `published` and `updated` when a date is explicitly supported by the paper.
- Preserve the supported precision: `YYYY` and `YYYY-MM` are valid when no month or day is established.
- Use an empty string for a date that cannot be established from the paper. Do not guess.
- Do not use the filename or directory name as evidence.
- Return only the structured result requested by the output schema.
