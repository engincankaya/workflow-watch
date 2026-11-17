export const meta = { name: 'bug-hunt', description: 'Find bugs in rounds until two rounds find nothing new; each new bug is judged by three lenses' }
const BUGS = { type: 'object', properties: { bugs: { type: 'array', minItems: 1, items: { type: 'object', properties: { id: { type: 'string' } } } } } }
const VERDICT = { type: 'object', properties: { real: { type: 'boolean' } } }
const FINDERS = ['logic', 'security', 'concurrency']
const LENSES = ['correctness', 'repro', 'impact']
const seen = new Set(), confirmed = []
let dry = 0, round = 0
while (dry < 2 && round < 3) {
  round++
  const found = (await parallel(FINDERS.map(k => () =>
    agent(`Find ${k} bugs that are not in this list: ${[...seen].join(', ')}`, { label: `find:${k}:r${round}`, phase: 'Find', schema: BUGS })))).filter(Boolean).flatMap(r => r.bugs)
  const fresh = found.filter(b => !seen.has(b.id))
  if (!fresh.length) { dry++; continue }
  dry = 0
  fresh.forEach(b => seen.add(b.id))
  const judged = await parallel(fresh.map((b, n) => () =>
    parallel(LENSES.map(lens => () => agent(`Judge bug ${b.id} through the ${lens} lens.`, { label: `judge:r${round}b${n}:${lens}`, phase: 'Judge', schema: VERDICT })))
      .then(votes => ({ b, real: votes.filter(Boolean).filter(v => v.real).length >= 2 }))))
  confirmed.push(...judged.filter(j => j.real).map(j => j.b))
}
return { confirmed: confirmed.length, rounds: round }
