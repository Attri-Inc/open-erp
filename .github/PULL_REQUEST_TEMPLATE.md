## Summary

<!-- What does this PR change, and why? -->

## Related issue

<!-- e.g. Closes #123 -->

## Checklist

- [ ] Tests added/updated for the change
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `mypy . --ignore-missing-imports` passes
- [ ] `pytest` passes
- [ ] `CHANGELOG.md` updated under `[Unreleased]`
- [ ] No core invariant weakened (balanced transactions, immutable postings, three-way match blocks the posting, corrections by contra posting only, AVCO recomputed on every receipt, same-transaction audit rows)
