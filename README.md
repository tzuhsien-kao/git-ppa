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
| `merge finish [package]` | After the merge is committed: choose the new version, add a changelog entry, check that the merge brought no new lint error, commit. `--dry-run` shows the version and the entry, `--me` signs as you instead of the profile identity, `--skip-ppa-check` for a PPA the tool cannot read |
| `lint [package]` | Check the packaging rules the profile enables |
| `orig [package]` | Fetch the orig tarball of the base version from Launchpad, verify its checksum, and name it for the downstream source |
| `login [package...]` | Store a Launchpad access token for private repositories, one at a prompt, or many from a JSON file or standard input with `--from-file` |
| `logout [package...]` | Remove stored tokens |
| `update-package [config.yaml]` | All the steps for one package in order: clone, merge, finish, build, upload to a staging PPA, wait for the build, then stop for review. See below |

Global options: `--profile FILE` (also `--skillset`, or `GIT_PPA_PROFILE`), `--series`, `--lp-user`
(also `--lpuser`, or `GIT_PPA_LPUSER`), `--json`, `-C DIR`.

Exit codes: 0 ok, 1 error or lint failure, 2 `status --check` found a package behind, 3 `merge start`
or `update-package` stopped with conflicts, 4 refused by a safety check, 5 `update-package` uploaded
and the build is not over yet.

### `update-package`

The same interface as the `update-package` draft of a shared packaging tool, for the part git-ppa
does: the base comes from git-ubuntu, the new version from the Ubuntu archive, the build happens in a
PPA. Every option can also be given in a YAML file with the same names, the command line wins.

```
git ppa update-package --skillset acme --pkg-name hello-acme --ppa-name acme-staging \
    --lp-user acme-bot --sign-key-file - --ssh-key-file ~/.ssh/acme-bot < signing.key
```

```yaml
# config.yaml
skillset: acme            # a profile file, or a name in the GIT_PPA_SKILLSETS directory
pkg-name: hello-acme
ppa-name: acme-staging    # must be in [safety] allow_ppas, and is never the package's target PPA
lp-user: acme-bot
manual-review: true       # the default
wait: 3600
```

- The run works in `./<package>` and refuses a directory that exists. It records each finished step
  in the repository, `--continue DIR` goes on from there: after conflicts were resolved and committed
  (exit 3), after a build that was still running (exit 5), or after an error. A version already in the
  staging PPA is not uploaded again.
- `merge finish` counts the versions of the target and of the staging PPA.
- It always stops after the build in the staging PPA. With `--manual-review` (the default) that is the
  review before `promote`, which is not implemented yet.
- `--base` other than `git-ubuntu`, `--from` other than `archive`, `--build` other than `ppa`, and
  `--ai-model`, `--review-model`, `--budget`, `--bug`, `--ppa-dependencies` are refused, not ignored.
  The staging PPA is not created, it must exist.

### How `merge start` judges a merge

Two separate answers, both in the report.

**Risk, high or low.** The merge is compared with the downstream tip from before it, line by line. It is
high risk if any changed line is in one of the sensitive regions of the profile, otherwise low:

- `debian/rules`: lines matching a `[risk] rules_markers` pattern, or inside an `ifeq` block that has one.
- `debian/control`: lines in a field listed in `control_fields` (the package renaming relations).
- Build system files named in `[risk] files` (for example `configure.ac`, `meson.build`), only when **our
  delta touches the same file**, directly or through one of its patches. The delta is everything that
  differs between the previous archive version (the `old/ubuntu` tag) and the downstream tip. Other changes
  to such files, for example an upstream version bump, are listed in the report as a note and not counted.
  Without `old/ubuntu` every such file counts and the report says so.

**Tier.** No conflict is `mechanical`. With conflicts, the `[caps]` of the profile decide: files, regions
and lines per side, `low` for a low risk merge and `high` for a high risk one, within them it is
`ai-assisted`, beyond them `re-port`. A high risk merge needs a human review in any case, so its cap only
limits what an automatic attempt may try first.

**A conflict in `debian/patches/series` is resolved by the tool** when it is a plain list merge: each side
may add and drop lines, repeated blank and comment lines are fine, and a moved line counts as a drop plus
an addition. It is not resolved when both sides drop or replace the same patch, when both add the same
patch in different places, or when the result would list a patch twice, those stay conflicts. The report
has a `resolved` line with the counts, and the patches still have to apply, which the build checks.

### How `merge finish` chooses the version

The new version must sort above the previous downstream version, and must never have been used in the
target PPA (Launchpad keeps a version number taken even after a deletion, so `merge finish` reads every
version the PPA ever had). The rule is `<archive version>+<tag>1`, or `~<tag>1` for a package that is not
in the archive, with the counter going up past used numbers.

That does not work when the archive appended letters to the old version, for example `5:8.0.5-1` to
`5:8.0.5-1ubuntu0.1` or `2.10-5` to `2.10-5build1`: dpkg sorts letters before `+`, so
`5:8.0.5-1ubuntu0.1+acme1` is lower than `5:8.0.5-1+acme5`. Then the old base is kept and only the counter
goes up (`5:8.0.5-1+acme6`), and the entry says so. The archive version a downstream is based on is
therefore read from its changelog (the newest entry of the archive source), never from its version.

`merge finish` refuses to commit when the changelog entry or the merge leaves a lint error that the
downstream did not have before the merge. Errors that were already there are listed as carried.

### Building, signing and uploading

```
git ppa build --sign-key-file KEY                      # signed source package next to the checkout
git ppa upload --lpuser <you> --ssh-key-file SSHKEY    # to the PPA named in the profile
```

The tool uses none of the keys of the machine it runs on. A strict snap cannot reach your keyring, agent
or `~/.ssh` anyway, so both keys are given to it:

- `--sign-key-file` is a secret key exported with `gpg --export-secret-keys <id>`, a file (mode 600) or
  `-` for standard input. It is imported into a temporary keyring that is deleted afterwards, the passphrase
  is read from `GIT_PPA_GPG_PASSPHRASE` and checked right away. Pass only the signing key.
- `--ssh-key-file` is the private key registered on your Launchpad account (OpenSSH or PEM format, the
  passphrase in `GIT_PPA_SSH_PASSPHRASE`), a file or `-`. There is no ssh executable and no agent, the upload
  is a small sftp client, and the host key of `ppa.launchpad.net` is pinned to the fingerprint that Launchpad
  publishes.

`build` stops when the top changelog entry is not one of the downstream source (run `merge finish` first),
when the tree is not clean, or when the version does not sort above the previous one. When dpkg fails the
tree is restored. `upload` refuses a `.changes` that is not signed or whose files do not match their
checksums, and **refuses a PPA that the profile does not name** under `[safety] allow_ppas`, so a profile
cannot write to a PPA by mistake. The files go in one by one and the `.changes` last. Launchpad uses a
version number once per PPA, even after a deletion, and mails the uploader when it rejects an upload.

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

[ppa]
owner = "acme-team"                # the package's ppa below is ~acme-team/ubuntu/<ppa>

[changelog]                        # who signs the entries of merge finish, a bot account
name = "Acme Bot"
email = "bot@example.com"

[safety]                           # every PPA upload may write to, nothing else is accepted
allow_ppas = ["~acme-team/ubuntu/acme-proposed"]

[[package]]
name = "hello-acme"
archive = "hello"
ppa = "acme-proposed"              # or a full reference, ~owner/ubuntu/name
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
- A connection that dies without being closed (a laptop that sleeps, a network change) is given up
  after a minute under 1 kB/s (`GIT_HTTP_LOW_SPEED_LIMIT` and `GIT_HTTP_LOW_SPEED_TIME` change it),
  and every git call stops after 15 minutes. Lock files that an interrupted run left in the `status`
  cache are removed once they are older than that.
- `status` skips the base version of packages marked `large` in the profile, add `--include-large`
  to read them, the first fetch is slow.
- `merge start` leaves a conflicted tree for you to resolve.
- A private PPA cannot be read by the tool yet, `merge finish` then needs `--skip-ppa-check` and does not
  know which versions the PPA has used.
- `upload --wait` reads a public PPA anonymously.

## Development

```
.venv/bin/python -m pytest -q
```

The tests use local fixture repositories and no network. Build the snap with `snapcraft pack`.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE). Copyright 2026 the git-ppa authors, as recorded in the git history.
