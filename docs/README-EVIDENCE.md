# Visual evidence

All original media below was supplied with the project. Filenames were shortened for stable repository-relative links; original slide content was preserved.

| File | Origin / what it establishes |
|---|---|
| [command-center.png](media/command-center.png) | Supplied GUI PNG: the remote-demo command center, service health, historical counts, live activity, and watch-only databases |
| [sentinel-demo.mp4](media/sentinel-demo.mp4) | Supplied MP4, 66.304 seconds: remote GUI, chaos controls, HTTP-failure incident, remediation, and resolved state |
| [incident-remediation.jpg](media/incident-remediation.jpg) | Frame extracted at 00:30: incident in remediation, reset-chaos action, and lifecycle timeline |
| [incident-resolved.jpg](media/incident-resolved.jpg) | Frame extracted at 00:55: the same incident in resolved state after validation |
| [architecture.jpg](media/architecture.jpg) | Original unnumbered JPG: full architecture presentation |
| [cicd.jpg](media/cicd.jpg) | Original JPG (1): delivery pipeline presentation |
| [workload-plane.jpg](media/workload-plane.jpg) | Original JPG (2): K3s workload-plane presentation |
| [evolution.jpg](media/evolution.jpg) | Original JPG (3): project history and direction |
| [deep-investigation.jpg](media/deep-investigation.jpg) | Original JPG (4): deep investigation and human authorization presentation |

The recording and screenshot are runtime evidence supplied by the owner. Slides communicate design and intent; they do not independently prove deployment success. The owner reports a working AWS deployment. No AWS account, CI history, or current live endpoint was accessed for this rewrite.

The timeline slide labels multi-environment support as current. The actual environment router replaces the active context, and the alert router explicitly lacks routing across environments. The README therefore documents one active environment and treats concurrent multi-environment operation as roadmap.

The CI slide summarizes an older conditional image/manifest path and suggests health checks on external Sentinel. Current workflow code always applies rendered workload manifests, pulls `latest` for external Sentinel, and checks systemd active state. The README follows code.

Dashboard percentages and times are historical demo values. The recording illustrates one HTTP-fault recovery; it is not a controlled performance evaluation or proof that all fault scenarios pass.

Media includes historical public IP addresses, incident IDs, and browser context. These are preserved as supplied evidence, not advertised as current service endpoints. The MP4 is linked as a repository file; GitHub's inline playback behavior may differ by view. The Mermaid diagrams are embedded in README Markdown and need no hosted diagram service.

