import { Bell, ClipboardList, FileText, HeartPulse, Hand, Microscope, Radar, RotateCcw, Route, Search, ShieldCheck, Waypoints, Wrench } from 'lucide-react'

const PHASE_ICONS = {
  detection: Radar,
  investigation: Search,
  correlation: Waypoints,
  root_cause_analysis: Microscope,
  remediation_decision: Route,
  policy_check: ShieldCheck,
  autonomous_execution: Wrench,
  recovery_validation: HeartPulse,
  re_investigation: RotateCcw,
  documentation: FileText,
  notification: Bell,
  learning: ClipboardList,
  escalation: Hand,
}

export function PhaseIcon({ phase, className }) {
  const Icon = PHASE_ICONS[phase] ?? Radar
  return <Icon aria-hidden="true" className={className} />
}
