# Publication checks

Before publishing, inspect the staged diff and scan both the current files and
the history being pushed. Ignore rules alone do not prevent forced staging,
hardcoded values in source files, or reintroducing old history.

The scanner checks private runtime paths, oversized files, common credential
formats, literal credentials, and any site-specific rules supplied privately.
It reports categories and paths, never matched values or source lines. Paths
containing a detected sensitive value are redacted too. Commit messages and
filenames are checked in addition to file contents.

Create a JSON policy outside this repository. Example schema:

```json
{
  "deny_patterns": ["internal[.]example[.]invalid"],
  "secret_globs": ["/private/deployment/secrets/*"]
}
```

Do not put actual domains, organization-specific phrases, keys, or private
policy contents into public scanner rules or test fixtures. Ensure policy and
secret files are readable only by the intended local account/group.

Enable the hooks on each clone:

```sh
chmod +x .githooks/pre-commit .githooks/pre-push
git config --local publication.policyFile /private/publication-policy.json
git config --local core.hooksPath .githooks
python3 tools/check_publication.py --ref HEAD --require-policy
```

The commit hook checks the full index, not just changed lines. The push hook
checks every commit and tree reachable from the outgoing tips, so deleting a
secret in a later commit does not make the push pass. Missing or invalid private
policy blocks these hooks. Update the private policy as deployment settings
change. Tests run in disposable repositories with synthetic data:

```sh
python3 -m unittest discover -s tools -p test_check_publication.py
```

Hooks are local and can be bypassed; they are a guard against mistakes, not an
access-control boundary or proof of absence of sensitive information. Other
clones must enable them separately. Run the generic scanner in independent
review/CI as another layer; site-specific checks require private rules there too.
Human review of public wording, commit messages, and file scope remains necessary.
