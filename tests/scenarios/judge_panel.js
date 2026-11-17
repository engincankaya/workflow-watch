export const meta = { name: 'plan-panel', description: 'Three plans from different angles, three judges, then refine or accept', phases: [{ title: 'Draft' }, { title: 'Judge' }, { title: 'Decide' }] }
const PLAN = { type: 'object', properties: { plan: { type: 'string' } } }
const SCORE = { type: 'object', properties: { winner: { type: 'integer' }, confident: { type: 'boolean' } } }
const drafts = await parallel([
  () => agent('Draft a plan that ships the smallest useful thing first.', { label: 'draft:mvp', phase: 'Draft', schema: PLAN }),
  () => agent('Draft a plan that removes the biggest risk first.', { label: 'draft:risk', phase: 'Draft', schema: PLAN }),
  () => agent('Draft a plan around what users do every day.', { label: 'draft:user', phase: 'Draft', schema: PLAN }),
])
const scores = await parallel(Array.from({ length: 3 }, (_, i) => () =>
  agent(`Score the three plans as judge ${i}.`, { label: `judge:${i}`, phase: 'Judge', schema: SCORE })))
const sure = scores.filter(Boolean).every(s => s.confident)
const final = sure
  ? await agent('Write the winning plan as the final plan.', { label: 'accept', phase: 'Decide' })
  : await agent('Merge the best ideas of all three plans.', { label: 'refine', phase: 'Decide' })
return final
