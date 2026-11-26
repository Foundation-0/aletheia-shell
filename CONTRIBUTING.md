# Contributing

## Sign-off (DCO)

Sign off every commit with `git commit -s`, which adds a
`Signed-off-by: Name <email>` trailer certifying the
[Developer Certificate of Origin](https://developercertificate.org/).

### Known gap: `bb5d80de`

`bb5d80de0711704d407659b0a005251d155a92d8` ("fix(boot): harden prep-boot-drive.sh
fail-closed gates", PR #2) has no `Signed-off-by` trailer. Its follow-up commits on that
branch (`23f7e29`, `4cb81ff`) are signed off. The commit is already published, so it is
recorded here instead of being rewritten: adding a trailer would change its hash and every
descendant's. Its changes to `bin/prep-boot-drive.sh` were reworked and signed off in
`4cb81ff` and in the issue #12 follow-up.
