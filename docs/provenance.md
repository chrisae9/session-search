# Source provenance

The Codex parser and its shared normalization module were extracted from the author's existing session-search skill. Its development history is recorded in the author's dotfiles repository at revision `dedc7faf083ff3d5cb6b06d6eddd6b8d19c5007a`; the Codex adapter was introduced by commit `f05e7cb`. Parser regression tests accompany the extraction.

The original tool is described by the author in [Building a Semantic Search Engine for Claude Code History](https://chis.dev/session-search). The initial shared parser definitions predate the Codex adapter; the dotfiles import itself did not contain a license file. Provenance review remains a release gate: confirm authorship or upstream permission for those definitions before public publication.

New application, storage, and interface code is developed for this repository under Apache-2.0. No code from the reviewed third-party memory projects has been copied. Dependencies retain their own licenses and require a distribution audit before release.
