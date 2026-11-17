export const meta = { name: 'write-and-check', description: 'A writer and a checker, at most three attempts', phases: [{ title: 'Write' }, { title: 'Check' }] }
const VERDICT = { type: 'object', properties: { ok: { type: 'boolean' } } }
const opts = (label, phase, schema) => ({ label, phase, schema })
let text = await agent(`Write the ${args.topic} page.`, opts(`write:${args.topic}`, 'Write'))
for (let attempt = 1; attempt <= 3; attempt++) {
  const verdict = await agent('Check the page.', opts(`check:${args.topic}:a${attempt}`, 'Check', VERDICT))
  if (verdict.ok) break
  text = await agent('Rewrite the page.', opts(`rewrite:${args.topic}:a${attempt}`, 'Write'))
}
return text
