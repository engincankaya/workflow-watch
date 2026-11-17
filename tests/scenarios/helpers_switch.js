export const meta = { name: 'mixed-shapes', description: 'Function declarations, switch, try/catch, do-while and then-chains', phases: [{ title: 'Prepare' }, { title: 'Run' }] }
async function prepare(part) {
  return agent(`Prepare the ${part} part.`, { label: `prepare:${part}`, phase: 'Prepare' })
}
await prepare('database')
await prepare('cache')
switch (args.mode) {
  case 'fast':
    await agent('Run the fast checks.', { label: 'run:fast', phase: 'Run' })
    break
  default:
    await agent('Run the full checks.', { label: 'run:full', phase: 'Run' })
}
try {
  await agent('Try the risky migration.', { label: 'migrate', phase: 'Run' })
} catch (e) {
  await agent('Roll the migration back.', { label: 'rollback', phase: 'Run' })
}
let attempt = 0
do {
  attempt++
  await agent('Poll the deploy status.', { label: `poll:${attempt}`, phase: 'Run' })
} while (attempt < 3)
const summary = await agent('Summarise the run.', { label: 'summary' }).then(s => agent('Shorten the summary.', { label: 'shorten' }))
return summary
