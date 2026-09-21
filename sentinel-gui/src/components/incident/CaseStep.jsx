import { Panel } from '@/components/sentinel/Panel'

/** One step of the case file: a numbered, titled panel (Evidence → Diagnosis → Decision → Result). */
export function CaseStep({ step, title, description, children, actions }) {
  return (
    <Panel
      title={
        <span className="flex items-center gap-2.5">
          <span aria-hidden="true" className="grid size-5 shrink-0 place-items-center rounded border bg-muted text-[11px] font-medium text-muted-foreground">
            {step}
          </span>
          {title}
        </span>
      }
      description={description}
      actions={actions}
    >
      {children}
    </Panel>
  )
}
