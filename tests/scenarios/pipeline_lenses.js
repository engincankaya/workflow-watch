export const meta = { name: 'lens-review', description: 'Each lens finds issues, a second agent checks them, a third ranks', phases: [{ title: 'Find' }, { title: 'Check' }, { title: 'Rank' }] }
const LENSES = args.lenses
const ISSUES = { type: 'object', properties: { issues: { type: 'array', minItems: 2, items: { type: 'string' } } } }
const results = await pipeline(LENSES,
  lens => agent(`Look at the code through the ${lens} lens and list issues.`, { label: `find:${lens}`, phase: 'Find', schema: ISSUES }),
  (found, lens) => agent(`Check each ${lens} issue: ${found.issues.join(', ')}`, { label: `check:${lens}`, phase: 'Check', schema: ISSUES }),
)
const ranked = await agent('Rank all checked issues by impact.', { label: 'rank', phase: 'Rank' })
return { ranked }
