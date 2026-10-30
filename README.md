# git-ppa

Keep a downstream PPA delta current with the Ubuntu archive.

`git-ppa` treats the Ubuntu archive as the upstream and a Launchpad PPA as the downstream. It finds
the newest version of a source package in the pockets you follow, merges it into the downstream git
branch, classifies the result by conflict size and by the regions it touched, and checks packaging
rules.

Everything specific to a project (package names, branches, PPAs, risk patterns, lint rules) lives in
a profile file that you pass with `--profile`, so the tool itself is not tied to one project.

**Status: prototype, phase 1.** The commands below only read from Launchpad and write to your local
clones. Nothing is pushed to Launchpad, uploaded or published by the tool.

## Install

As a snap (strict confinement, git and `dpkg-mergechangelogs` are inside):

```
sudo snap install git-ppa --edge
```

The snap is on the `edge` channel only for now. It reaches Launchpad over https, so a downstream
that needs credentials must be read with an access token (see "Private repositories"), and it can
only write to non-hidden files and directories under your home directory, so work in a directory
there (not `/tmp`). To try a local build instead: `sudo snap install --dangerous git-ppa_*.snap`.

From a checkout (needs `git`, `dpkg-dev` and `ssh` on the host):

```
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
```

A checkout uses the host's git and ssh, so a profile with a `git+ssh://{lpuser}@git.launchpad.net/...`
repository URL works there with your ssh key and agent, without any token.

Run it as `git-ppa <command>` or `git ppa <command>`. Use `-h` for help, `git ppa --help` is
intercepted by git.

## Commands

| Command | What it does |
|---------|--------------|
| `status [package...]` | How far behind the followed pockets each downstream is, with the pocket and an approximate CVE count from the changelogs. `--check` exits 2 if any package is behind |
| `clone <package> [dir]` | Clone the downstream repository, add the Ubuntu import as a remote, register `dpkg-mergechangelogs` as the merge driver for `debian/changelog` |
| `sync [package]` | Fast-forward the local mirror branch to the import tag of the newest followed version |
| `merge start [package]` | Merge the archive import tag into the downstream branch on a staging branch, report the tier (mechanical, ai-assisted, re-port) and the risk, leave conflicts in the tree |
| `lint [package]` | Check the packaging rules the profile enables |
| `orig [package]` | Fetch the orig tarball of the base version from Launchpad, verify its checksum, and name it for the downstream source |
| `login [package...]` | Store a Launchpad access token for private repositories, one at a prompt, or many from a JSON file or standard input with `--from-file` |
| `logout [package...]` | Remove stored tokens |

Global options: `--profile FILE` (or `GIT_PPA_PROFILE`), `--series`, `--lpuser` (or
`GIT_PPA_LPUSER`), `--json`, `-C DIR`.

Exit codes: 0 ok, 1 error or lint failure, 2 `status --check` found a package behind, 3 `merge start`
stopped with conflicts, 4 refused by a safety check.

## Profile

A TOML file. A minimal one:

```toml
[project]
tag = "acme"                       # suffix of versions and binaries, +acme1
series = ["resolute"]
pockets = ["Release", "Security", "Updates"]
notify_pockets = ["Proposed"]

[repo]
url = "https://git.launchpad.net/~acme-team/acme/+git/{name}"
upstream_url = "https://git.launchpad.net/ubuntu/+source/{archive}"

[branches]
mirror = "ubuntu/{series}"
downstream = "{tag}/{series}"
staging = "{tag}-proposed/{series}"

[risk]
# regular expressions, matched on the lines a merge changed in debian/rules
rules_markers = ['^\s*(export\s+)?(CC|CXX|CFLAGS)\s*[:+?]?=']
control_fields = ["Provides", "Conflicts", "Replaces"]
files = ["configure.ac", "*.m4"]

[rules]
enable = ["version-order", "binary-rename", "no-stale-names", "no-empty-packages"]

[[package]]
name = "hello-acme"
archive = "hello"
```

Templates may use `{tag}`, `{series}`, `{name}`, `{archive}` and, for ssh URLs, `{lpuser}`.

## Private repositories

Launchpad accepts an access token as the https password, together with your Launchpad user name.
Create a token with the `repository:pull` scope on the repository's "Manage access tokens" page
(`https://launchpad.net/<owner>/<project>/+git/<repository>/+access-tokens`) and store it:

```
git ppa login --lpuser <you> <package>
```

A token is made for one repository, so each package needs its own, unless you have a project token.

### Many tokens at once

Keep them in one JSON object of package to token. The key is a package name of the profile (or its
Ubuntu source name), `*` is a token for every package that is not named:

```json
{
  "hello-acme": "<token for hello-acme>",
  "other-acme": "<token for other-acme>",
  "*": "<project token, used for every package not named above>"
}
```

A file or a CI secret has the same format. Import it:

```
git ppa login --lpuser <you> --from-file tokens.json      # a file, mode 600, outside hidden directories
printf '%s\n' "$LP_TOKENS" | git ppa login --lpuser <you> --from-file -    # standard input, for CI
```

A duplicate key, a package the profile does not know, an empty token or a token with spaces rejects
the whole input, nothing is imported half way. A file that other users can read is refused. Each
token is tried on its repository and kept only if that works, and a refused token never replaces a
working one. Delete the file afterwards. Tokens are then kept in a file readable only by you and
handed to git through a credential helper, never on a command line or in a remote URL.

### CI

Put the JSON in one secret and import it with the `-` form above, or for one token that covers every
repository set `GIT_PPA_TOKEN` and `GIT_PPA_LPUSER` and skip `login`, nothing is then written to disk.
The snap can only read files under the home directory, so use a workspace there.

## Known limits

- Launchpad errors that go away on their own (an https 502, an ssh connection closed) are not
  retried, run the command again. Over `git+ssh` Launchpad refuses several parallel connections,
  use `status --jobs 1`.
- `status` skips the base version of packages marked `large` in the profile, add `--include-large`
  to read them, the first fetch is slow.
- `merge start` leaves a conflicted tree for you to resolve. `merge finish`, building and uploading
  are not implemented.

## Development

```
.venv/bin/python -m pytest -q
```

The tests use local fixture repositories and no network. Build the snap with `snapcraft pack`.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE). Copyright 2026 the git-ppa authors, as recorded in the git history.
