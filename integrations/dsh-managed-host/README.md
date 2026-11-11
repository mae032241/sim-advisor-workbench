# Managed DSH Host

This task-owned ordinary Host plugin is loaded with `dsh sdk --patch` in one
dedicated workbench workspace. It uses the full SDK profile's native coding,
filesystem, persistent shell, model adapters and session persistence. No bundle
installation or edits/copies of the user's profile, HOME or credentials occur.

Python supplies a loopback callback origin and random token through two private
child environment values. The plugin captures and removes these values before
creating an Agent, so native shell children do not inherit the bridge token.
Configuration, visible events and tool calls use authenticated private HTTP.
The helper's own loopback control server implements send, interrupt and close
using public Agent followup/steer/cancel/dispose APIs. SDK JSON-RPC is not extended
or falsely described as supporting those operations.

Start validates the selected provider/model and creates or explicitly resumes a
session without sending a prompt. The setup callback adds scoped planning tools
and the independent `sim-advisor:managed-task` prompt section. Existing SDK tools
and prompt sections remain. Resume failures are errors, never a fresh session.
Only visible text and state are mirrored; reasoning, SDK raw stdio and raw native
errors are not sent to the workbench. Stdout/stderr are drained by Python in fixed
chunks without retaining them. The runtime closes only its own process tree.

`SIM_ADVISOR_DSH` can select the official CLI/shim. Optional
`SIM_ADVISOR_DSH_PROVIDER` defaults to `deepseek-official`. Native runtime smoke
validation is separate from the mock tests and does not run during package tests.

Only a successful native `session/event` `turn/end` publishes the latest visible
assistant message from that turn. Intermediate/tool/reasoning/token output is
not mirrored; canceled/failed/incomplete turns never publish an earlier message
as final. Native coding/planning tools and send/stop/disconnect remain available.
