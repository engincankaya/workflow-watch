// No meta phases, no labels: the viewer has only the prompts to go by.
export const meta = { name: 'no-labels', description: 'Agents without labels or phases' }
const outline = await agent('Write an outline for the onboarding guide.')
const [intro, setup] = await Promise.all([
  agent('Write the introduction section of the onboarding guide.'),
  agent('Write the setup section of the onboarding guide, with every command.'),
])
const reviewSection = (name) => agent(`Proofread the ${name} section and fix the wording.`)
await reviewSection('introduction')
await reviewSection('setup')
const verdict = await agent('Decide if the guide is ready to publish.', { schema: { type: 'object', properties: { ready: { type: 'boolean' } } } })
if (!verdict.ready) {
  await agent('List what still blocks the guide from being published.')
}
return 'done'
