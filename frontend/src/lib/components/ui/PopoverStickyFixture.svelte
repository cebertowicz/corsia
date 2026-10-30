<script lang="ts">
 import Popover from './Popover.svelte';
 let open = $state(true);
 let key = $state('first');
 export function setKey(value: string) { key = value; }
 export function setOpen(value: boolean) { open = value; }
</script>

<Popover anchor={new DOMRect(400, 400, 50, 20)} {open} onClose={() => { open = false; }} placement="bottom" keepSide sideKey={key}>
 <span>Comment</span>
</Popover>
