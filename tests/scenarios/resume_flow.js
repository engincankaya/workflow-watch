export const meta = { name: 'resume-me', description: 'Two fixed steps and one that depends on the input, to resume' }
await agent('Collect the facts.', { label: 'collect' })
await agent('Draft the answer.', { label: 'draft' })
await agent(`Polish the answer, version ${args.v}.`, { label: 'polish' })
return 'ok'
