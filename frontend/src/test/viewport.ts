// jsdom has no matchMedia. The app only asks whether the viewport is md or
// wider; tests get a desktop unless they ask for a phone.
let wide = true

/** Makes matchMedia answer as a phone would, until the test ends. */
export function onPhone() {
  wide = false
}

export function resetViewport() {
  wide = true
}

window.matchMedia = (media: string) => ({
  matches: wide, media, onchange: null,
  addEventListener: () => {}, removeEventListener: () => {},
  addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
})
