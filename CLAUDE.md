## Approach
- Read existing files before writing. Don't re-read unless changed.
- Thorough in reasoning, concise in output.
- Skip files over 100KB unless required.
- No sycophantic openers or closing fluff.
- No emojis or em-dashes.

## Verification
- Do not guess APIs, versions, flags, commit SHAs, or package names. Verify by reading code or docs before asserting.

## Comments
- Inline comments say why or what constrains the code, not what it does — that's visible by reading it.
- Put a comment next to what it describes.
- Don't name other files or functions unless the link is load-bearing (e.g. a format both sides must keep in sync). Describe local behavior instead.
- State each fact in one place only.
- One short sentence per idea; no run-ons.
- Avoid documenting things likely to change soon; they go stale fastest.
- TODO items, open questions, and pending decisions go in TODO.md, not in comments.
- When reviewing comments, point out what's wrong and propose a tighter rewrite.
- Don't delete comments that explain real logic or business rules.

## Docstrings
- Function docstring: 1 sentence ideally, 2 if needed, 3 absolute max.
- Say what the function is for, not a step-by-step of its body.
- Per-function description belongs in the docstring, not scattered inline comments.
- No Args/Returns/Raises block unless a param or return type is genuinely unclear from the signature.
- Don't restate the function name or document obvious types.
- Module docstring: 1-2 sentences — what it is and how to use it.
- If the docstring would just paraphrase the signature, omit it.