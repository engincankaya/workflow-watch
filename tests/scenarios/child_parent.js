export const meta = { name: 'parent-flow', description: 'Write a page, then run a child workflow on it', phases: [{ title: 'Write' }] }
const RULE = 'Write plainly, in short sentences. '
const page = await agent(RULE + 'Write the release notes page.')
const checked = await workflow('child-flow', { page })
await agent(RULE + 'Publish the page.', { label: 'publish', phase: 'Write' })
return checked
