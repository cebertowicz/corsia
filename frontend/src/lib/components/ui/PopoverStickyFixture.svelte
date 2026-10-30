<script lang="ts">
 import Popover from './Popover.svelte';
 let open = $state(true);
 let key = $state('first');
 let rect = new DOMRect(400, 400, 50, 20);
 const anchor = () => rect;
 export function setKey(value: string) { key = value; }
 export function setOpen(value: boolean) { open = value; }
 export function setRect(value: DOMRect) { rect = value; }
</script>

<Popover {anchor} {open} onClose={() => { open = false; }} placement="bottom" sideKey={key}>
 <textarea aria-label="Comment"></textarea>
</Popover>
