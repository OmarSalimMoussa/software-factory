# Signing in as a test address

How a tester signs in on a slot. Only test addresses are used, never a real person's account.

- **Which address.** Your brief names it. The product's `qa/scenarios/README.md` says which test address plays which
  role. Testers that run at the same time each sign in as their own address, so their data never clash.
- **The link.** `factory slot sign-in <address>` prints a one-time sign-in link for the slot your session holds.
  The product decides how long it lasts. Ask for a new one each time you need it.
- **In a browser:** open the link with your browser tools. To change person, open a fresh link: it replaces the
  sign-in in that browser.
- **Through the API:** open the link with the `slot-api` tools and keep the session they hand back for the
  requests that follow.
- **A first sign-in** may land on a setup screen the product shows new people. Follow it if the scenario expects
  it; otherwise report the step blocked with what you saw.
- **Never** put a link, a password or a session token in your report: they are secrets.
