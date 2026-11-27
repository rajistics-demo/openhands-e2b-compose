# OpenHands Canvas demo

Start with an isolated installation and a disposable repository. The
[Compose quickstart](SETUP.md#build-and-start) covers the packaged E2B setup.
The walkthrough records a mixed-provider Enterprise candidate tested on
AWS Compose with E2B and a Kubernetes sandbox controller. It requires the
candidate's provider router and Canvas controls.

## Show the workspace choices

1. Open Canvas and choose **E2B Standard**, **E2B Large**, **Kubernetes
   Standard**, or **Kubernetes Large** in the **Sandbox Spec** picker. An
   OpenHands Agent Profile can also store a spec under **Settings → Agent
   Profiles → Edit**. The template and image are resolved by Enterprise
   from the permitted catalog.
2. Open a running chat. In its sidebar **More options** menu, choose **Use
   current files** to start a blank chat with the original workspace.
   Uncommitted tracked edits and untracked files are visible in both chats.
3. Choose **Separate checkout** to start a blank chat from the current
   commit. It creates a Git worktree on the same machine. The working
   files and index are independent, and both chats share the machine
   and its security boundary.
4. Choose **Separate machine**, select a spec, and click **Start blank
   chat**. Verify that the new chat reaches an editable composer and binds
   to a different sandbox. This dialog was verified with both providers.
5. In an idle E2B chat, click **Pause** and wait for the sandbox to settle.
   Reopen the original conversation and verify its sandbox ID, history,
   and workspace marker. A new chat in a running sandbox creates a new
   conversation ID. Reopening continues the original conversation.

The live Compose policy was set to pause after 20 minutes idle, pause
E2B at the end of its one-hour running lease, and delete after 10 days
without running. A paused E2B session can show a generic **Disconnected**
warning. Check the provider state before concluding that the workspace
disappeared. The mixed-provider demo supports same-VM resume. Recovery
after deletion requires the separate checkpoint candidate below.

## Show a real coding task and an automation

Select the same approved model profile on both providers. In the smoke
test, DeepSeek V4.1 Flash fixed a small Python normalization function on
E2B Standard and Kubernetes Standard. Each workspace started from the
same files with one failing test. Both agents edited the implementation,
ran three passing tests, and passed a separate rerun.

On the combined automation candidate, edit an automation and use **Run
startup → Agent Profile** to choose a profile with a Sandbox Spec. Each
run allocates a separate sandbox and starts a new chat. The manual-only
**Template ready** smoke completed on Kubernetes. E2B placement reached
the expected spec, and that automation runner did not complete its SDK
metadata fetch. Prepared-workspace and pre-started-conversation claims
remain future automation work.

## Timing the three startup paths

Time until the chat composer is editable. Seed a prepared repo before
timing when measuring **Use current files**. Check that each successful
run used the original sandbox ID and kept a tracked edit and an untracked
file. For warm claims, verify the allocated ID was already in ready
inventory. For cold launches, verify an inventory miss or disable the
pool in the disposable fixture.

| Path | E2B | Kubernetes |
| --- | --- | --- |
| New chat on an existing prepared sandbox | 0.85 s median, 0.77–0.92 s, 10 successes on the E2B prototype | 0.94 s median, 0.89–1.61 s, 10 successes and one timeout on the Compose demo |
| New chat with a warm allocation | 7.08–7.16 s, two observations | Editable by 9.72 s and 12.09 s, two EKS observations |
| New chat with a cold allocation | 9.44 s, one observation | 26.46–30.08 s EKS readiness window, one observation |
| Reopen a running chat with history | 3.48 s, one Compose observation | 2.60 s, one Compose observation |

The prepared-sandbox medians come from separate builds. A later E2B
repeat on the mixed demo did not produce a comparable batch. The warm
and cold rows come from the earlier EKS comparison on different
deployments. The experiment used the same Agent Server release and model,
with different Canvas and Enterprise builds. The EKS warm times are
upper bounds because the browser attached after allocation. The cold
EKS window is bounded by the last disabled and first editable observations.
These small samples describe the demo paths.

## Separate E2B lifecycle prototype

The [pinned Canvas overlay](../canvas/README.md) demonstrates
authenticated previews and can expose **Fork into a new VM** with a
compatible Enterprise candidate. A fork uses its own VM and independent
files while carrying the source chat history and workspace. Keep the
source active and confirm its sandbox binding and files after the fork.
[Native lifecycle validation](SETUP.md#qualify-native-e2b-lifecycle)
exercises independent provider forks as a separate test.

In the expiry-recovery prototype, an automatic checkpoint saved a
synthetic repo before the original VM expired. A read against the
provider confirmed the VM was gone. Canvas kept the conversation ID and
restored its tracked edit, untracked file, preview, and history onto a new
VM. A follow-up native turn could edit the restored files. In that single
run, history was visible in **0.54 s** and the replacement preview first
responded in **8.93 s**. Enterprise and Canvas recovery support are both
required for this path.
