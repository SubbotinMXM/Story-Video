/** Переход между разделами из любой карточки: App слушает событие и переключает страницу. */
export type PageId = "steps" | "templates" | "broll" | "slides" | "covers" | "runs" | "copies" | "analytics" | "comments" | "polls" | "niches" | "settings";

export function goTo(page: PageId, anchor?: string) {
  window.dispatchEvent(new CustomEvent("sv-goto", { detail: { page, anchor } }));
}
