export const meta = { name: 'child-flow', description: 'A child workflow: two checks in parallel' }
await parallel(['format', 'links'].map(k => () => agent(`Check the ${k} of the page.`, { label: `child:${k}` })))
return 'checked'
