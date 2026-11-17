export const meta = { name: 'big-sweep', description: 'One agent per item, more items than the flow shows as columns' }
const n = args.n
const results = await parallel(Array.from({ length: n }, (_, i) => () => agent(`Check item ${i}.`, { label: `item:${i}`, phase: 'Sweep' })))
await agent('Merge all item results.', { label: 'merge', phase: 'Merge' })
return results.length
