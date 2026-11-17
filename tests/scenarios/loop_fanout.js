export const meta = { name: 'two-round-review', description: 'Two rounds; each round reviews three files in parallel, then one fixer works' }
const FILES = ['a.py', 'b.py', 'c.py']
for (let round = 1; round <= 2; round++) {
  await parallel(FILES.map(f => () => agent(`Review ${f}, round ${round}.`, { label: `review:${f}:r${round}`, phase: 'Review' })))
  await agent(`Fix what round ${round} found.`, { label: `fix:r${round}`, phase: 'Fix' })
}
return 'ok'
