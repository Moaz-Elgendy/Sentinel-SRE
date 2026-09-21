import { ArrowRight, ShieldOff } from 'lucide-react'
import { Fragment } from 'react'
import { applyRemediationChange, getRemediationConfig, previewRemediationChange } from '@/api/config'
import { ConfigPage, ReadOnlyPanel, SettingRow, SettingsSection } from '@/components/config/ConfigShell'
import { Callout } from '@/components/sentinel/States'
import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useConfigEditor } from '@/hooks/useConfigEditor'
import { usePageTitle } from '@/hooks/usePageTitle'
import { actionLabel, rootCauseLabel } from '@/utils/incident'

// One editable setting (dry run). It is changed through the same review → confirm
// flow as everything else, started directly from the switch.
const makeDraft = (current) => ({ dry_run: current.dry_run })
const diffChanges = () => ({})

export default function RemediationConfigPage() {
  usePageTitle('Remediation configuration')
  const editor = useConfigEditor({ load: getRemediationConfig, previewChange: previewRemediationChange, applyChange: applyRemediationChange, makeDraft, diffChanges, loadErrorMessage: 'Could not load remediation configuration.' })
  const readOnly = editor.data?.read_only
  const dryRun = editor.data?.current.dry_run

  return (
    <ConfigPage editor={editor} noActionBar description="How Sentinel applies the actions it decides on." appliedMessage="Configuration applied. Sentinel is using the new mode now.">
      {editor.data && (
        <>
          <SettingsSection title="Dry run" description="When on, Sentinel decides and authorizes actions exactly as normal but applies none of them to the cluster. Evidence, diagnosis, policy checks and the audit trail all still run.">
            <SettingRow label={dryRun ? 'Dry run is on' : 'Sentinel is applying actions'} hint={dryRun ? 'No changes are being made to the cluster. Turn it off to let Sentinel remediate.' : 'Turn dry run on to observe decisions without any risk to the cluster.'}>
              <div className="flex items-center gap-3">
                <Switch checked={dryRun} onCheckedChange={() => editor.review({ dry_run: !dryRun })} disabled={editor.reviewing} aria-label="Dry run mode" />
                <Badge variant={dryRun ? 'warn' : 'neutral'}>{dryRun ? 'Dry run' : 'Autonomous'}</Badge>
              </div>
            </SettingRow>
          </SettingsSection>
          {!dryRun && (
            <Callout tone="neutral" icon={ShieldOff}>
              Toggling this opens a review first. Nothing changes until you confirm.
            </Callout>
          )}
          <ReadOnlyPanel title="Action ladder" description={readOnly.description}>
            <div className="overflow-hidden rounded-md border">
              <Table>
                <caption className="sr-only">Candidate remediation actions per root cause</caption>
                <TableHeader>
                  <TableRow className="hover:bg-transparent">
                    <TableHead>When the diagnosis is</TableHead>
                    <TableHead>Sentinel tries, in order</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {Object.entries(readOnly.action_ladder).map(([rootCause, actions]) => (
                    <TableRow key={rootCause}>
                      <TableCell>{rootCauseLabel(rootCause)}</TableCell>
                      <TableCell>
                        {actions.length > 0 ? (
                          <span className="inline-flex flex-wrap items-center gap-1.5">
                            {actions.map((action, i) => (
                              <Fragment key={action}>
                                {i > 0 && <ArrowRight aria-label="then" className="size-3 text-muted-foreground" />}
                                <Badge variant="secondary" className="font-normal">
                                  {actionLabel(action)}
                                </Badge>
                              </Fragment>
                            ))}
                          </span>
                        ) : (
                          <span className="text-muted-foreground">Never remediated: always escalates to an SRE</span>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
            <p className="mt-2 text-xs text-muted-foreground">
              Each step down the ladder discounts confidence by <span className="tnum font-medium text-foreground">{readOnly.fallback_confidence_discount}</span>, so a fallback needs stronger evidence to pass policy.
            </p>
          </ReadOnlyPanel>
        </>
      )}
    </ConfigPage>
  )
}
