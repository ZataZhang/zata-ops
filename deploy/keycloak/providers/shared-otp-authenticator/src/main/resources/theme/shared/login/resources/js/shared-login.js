let submitting = false

document.addEventListener("submit", (event) => {
  if (!event.target.matches(".shared-login-form, .shared-login-resend")) return
  if (submitting) {
    event.preventDefault()
    return
  }
  submitting = true
  for (const button of document.querySelectorAll(".shared-login-form button, .shared-login-resend button")) {
    button.setAttribute("aria-disabled", "true")
  }
})

window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return
  submitting = false
  for (const button of document.querySelectorAll(".shared-login-form button, .shared-login-resend button")) {
    button.removeAttribute("aria-disabled")
  }
})
