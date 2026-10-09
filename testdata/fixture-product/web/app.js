import { warn } from "./log.js";

export function drawNotes(list, notes) {
  list.replaceChildren(...notes.map((note) => {
    const item = document.createElement("li");
    item.textContent = note.text;
    return item;
  }));
}

export function drawFailure(banner, sentence) {
  banner.textContent = sentence;
  warn("a save failed");
}
