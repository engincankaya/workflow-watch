export const meta = {
  name: 'route-audit',
  description: 'Audit every route file for missing auth checks, then try to refute each finding',
  phases: [{ title: 'Scan' }, { title: 'Audit' }, { title: 'Verify' }],
}
const FILES = { type: 'object', properties: { files: { type: 'array', minItems: 12, items: { type: 'string' } } } }
const FINDINGS = { type: 'object', properties: { findings: { type: 'array', minItems: 1, items: { type: 'object', properties: { title: { type: 'string' } } } } } }
const VERDICT = { type: 'object', properties: { real: { type: 'boolean' } } }

phase('Scan')
const scan = await agent('List every route file under src/routes.', { label: 'scan', schema: FILES })
phase('Audit')
const audits = await parallel(scan.files.map((f, i) => () =>
  agent(`Audit route file number ${i} for missing auth checks.`, { label: `audit:${i}`, phase: 'Audit', schema: FINDINGS })))
phase('Verify')
const all = audits.filter(Boolean).flatMap(a => a.findings)
const verdicts = await parallel(all.map((f, i) => () =>
  agent(`Try to refute this finding: ${f.title}`, { label: `verify:${i}`, schema: VERDICT })))
log(`${verdicts.filter(v => v && v.real).length} findings survived`)
return { confirmed: verdicts.filter(v => v && v.real).length }
