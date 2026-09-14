<!-- Thanks for contributing to Punctim! See CONTRIBUTING.md. -->

## What & why

<!-- What does this change, and why? Link any related issue (e.g. "Closes #123"). -->

## How tested

<!-- Commands you ran and their result. -->

## Checklist

- [ ] Branched off `main`; the change is focused.
- [ ] Matches the style of the surrounding code.
- [ ] Ran the relevant per-language tests.
- [ ] **If I touched the wire/audio path:** regenerated the golden vectors and ran
      `make certify` (or the Python/Rust/C certs) — they pass, and the
      `Documentation/` + `python/MCP/` vector copies are identical.
- [ ] No new wire format and no encryption added to the core wire path.
- [ ] New files carry the appropriate `SPDX-License-Identifier` (LGPL-3.0-only for
      the library).
