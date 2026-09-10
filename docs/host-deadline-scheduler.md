# Host deadline scheduler

Deadline-bound EC2 provisioning requires two independently persisted controls. EC2 user
data installs an absolute timer on the worker before bootstrap starts. The controller also
arms an external user scheduler before calling `RunInstances`.

`LocalDeadlineScheduler` supports Linux/WSL user systemd and macOS launchd. Linux setup must
enable user lingering for the controller account and provide `systemctl --user` during
unattended operation. macOS requires a logged-in LaunchAgent domain. Both run
`python -m garden.hosts.deadline_scheduler --execute RECEIPT`; the saved receipt contains
only host identity, deadline, tags, AWS profile, region, and admitted account ID. At
execution, STS must return that exact account and the assumed
`ContextGardenProvisioner` role ARN before EC2 is read.
The AWS profile must already have narrowly scoped describe and terminate permission for
the owned Garden instances.
No administrator or policy-write permission is required.

The Linux adapter verifies lingering and the effective loaded unit contents; it does not
enable lingering itself. The macOS interval checker can run up to 60 seconds after the
absolute deadline. The worker's own timer remains the exact-deadline control.

Windows without WSL has no built-in manager. Configure `deadline_command` as an argv list
whose independently installed helper durably arms and reads back Windows Task Scheduler,
then returns Garden's exact verified receipt. Provisioning fails closed when neither a
supported user scheduler nor that configured command is available.

The executor filters by managed, owner, pool, host, and operation tags and rechecks every
returned instance before termination. Receipts are immutable per operation. Keep the state
directory private (0700 and files 0600), keep the Python environment and AWS profile valid
through the deadline, and test the user scheduler after OS upgrades.
