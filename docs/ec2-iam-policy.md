# IAM policy for an EC2 host

`ec2-iam-policy.json` is the least-privilege policy for the IAM user that Own Compute uses for
an EC2 host. Make a user only for this plugin (never use the root user), attach this policy as a
**customer managed policy** (the file is about 3,300 characters without spaces, more than the
inline limit), and store that user's access key in the Hermes secrets `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`.

Before you use it, change two things:

- **Region.** Every `us-east-1` is a placeholder. Replace it with the region of the host.
- **Instance types.** The two types in the file (`t3.micro`, `t3.small`) are an example, listed
  twice (an allow and a deny). Put in the types you are willing to pay for. The Add host wizard
  can list the types AWS reports as free-tier eligible for the account; check the AWS billing
  page for what that covers. The policy also limits the root disk to 30 GB.

What it allows:

- Read-only `Describe` calls. AWS cannot limit these by tag, so they can list everything in the
  region (they change nothing).
- `ssm:GetParameter` on the public image parameters only (latest Amazon Linux 2023 and Ubuntu).
- `ec2:RunInstances` for the listed types, only when the request tags the instance and its disk
  with `cloud-compute:install`. The image, subnet, network interface, security group and key pair
  used by a launch are existing resources that AWS cannot limit by tag.
- Start, stop, terminate and console output only on instances that carry the tag. Key pairs and
  security groups are created only with the tag and changed or deleted only when they carry it.
- Explicit denies for other instance types and for every other region.

The tag condition checks that the tag key exists, not its value. Anything without that key
cannot be started, stopped, terminated or changed with this key.

This policy limits what the key can launch. It is not a spending cap: add a budget with an alert
in the AWS billing console as well.
