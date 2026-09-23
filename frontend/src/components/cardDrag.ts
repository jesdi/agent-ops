import type { DragEvent } from 'react'

export interface DraggedCard {
  issue: number
  target: string
  title: string
}

const CARD_MIME = 'application/x-agent-ops-card'

/** Drag-source props that carry the card payload. Shared by task and ghost
 *  cards, so every draggable card writes the same payload. */
export function cardDragSource(card: DraggedCard) {
  return {
    draggable: true,
    onDragStart: (e: DragEvent) => {
      e.dataTransfer.setData(CARD_MIME, JSON.stringify(card))
    },
  }
}

/** Drop-target handlers for the card drag payload. Shared by the Wont do
 *  column and its count-strip chip, so both accept the same drags. */
export function cardDropTarget(onCardDrop: ((card: DraggedCard) => void) | undefined) {
  if (!onCardDrop) return {}
  return {
    onDragOver: (e: DragEvent) => e.preventDefault(),
    onDrop: (e: DragEvent) => {
      e.preventDefault()
      const raw = e.dataTransfer.getData(CARD_MIME)
      if (!raw) return
      try {
        onCardDrop(JSON.parse(raw) as DraggedCard)
      } catch { /* foreign drag — ignore */ }
    },
  }
}
