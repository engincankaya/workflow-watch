export const meta = { name: 'per-file-checks', description: 'For each file: review it, then run three checks on it in parallel' }
const FILES = ['api.py', 'db.py', 'ui.tsx', 'jobs.py']
const CHECKS = ['lint', 'types', 'tests']
const out = await parallel(FILES.map(f => async () => {
  const review = await agent(`Review ${f}.`, { label: `review:${f}`, phase: 'Review' })
  const checks = await parallel(CHECKS.map(c => () => agent(`Run the ${c} check on ${f}.`, { label: `check:${f}:${c}`, phase: 'Check' })))
  return { f, review, checks }
}))
return out.length
