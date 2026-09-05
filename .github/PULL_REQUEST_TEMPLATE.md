<!--
Thanks for contributing to TrajWeave. Keep the PR focused on one change.
For anything user-visible, update README.md in the same PR.
Security fixes: coordinate privately first (see SECURITY.md).
-->

## Summary

<!-- What does this PR do, in one or two sentences? -->

## Why

<!-- The motivation. Link the issue it closes: "Closes #123". -->

## Changes

<!-- The user-visible changes. Bullet points are fine. -->

## Testing

<!-- Commands you ran and their result. -->

```
uv run pytest
uv run ruff check .
```

## Documentation

<!-- README / docs updates, or "not needed because ...". -->

## Compatibility / migration impact

<!--
Call out any of the following, or write "none":
- CLI surface changes (new/renamed/removed commands or flags)
- database schema migration added
- change to persistence, lifecycle, redaction, or apply-safety semantics
- change to serialization / on-disk formats
- backward-incompatible changes
-->

## Checklist

- [ ] The PR is focused on a single change.
- [ ] Tests were added or updated for new/changed behavior (regression test for a bug fix).
- [ ] `uv run pytest` passes locally.
- [ ] `uv run ruff check .` passes locally.
- [ ] `README.md` / `docs/` updated if behavior changed.
- [ ] No secrets, credentials, or real transcript data are included.
- [ ] Backward compatibility (CLI, schema, persisted data) was considered and is noted above.
