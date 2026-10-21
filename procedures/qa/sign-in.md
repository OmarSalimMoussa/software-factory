# Signing in as a test address

How a tester signs in on a slot. Only test addresses are used, never a real person's account.

- **Which address.** Your brief names it. The product's `qa/scenarios/README.md` says which test address plays which
  role. Testers that run at the same time each sign in as their own address, so their data never clash.
- **In your browser:** `browser_sign_in` with the address. It asks your slot for a fresh one-time link and opens it,
  and you never see the link. To change person, call it again with the other address. Each page answer of that
  browser says who the factory last signed it in as.
- **Through the API,** for a tester with the `slot-api` tools: `factory slot sign-in <address>` prints a one-time
  link for the slot your session holds. Open its path with the `slot-api` tool, naming your address as its `session`,
  and name that `session` on every request that follows: the tool keeps the cookies the slot sets under it and never
  shows their values. Ask for a new link each time you need one; the product decides how long it lasts.
- **On the remote desktop:** `factory slot sign-in <address>` prints the link; type it into the desktop's browser.
  The replay hides the screen for a while after a link is typed.
- **A first sign-in** may land on a setup screen the product shows new people. Follow it if the scenario expects
  it; otherwise report the step blocked with what you saw.
- **Never** put a link, a password or a session token in your report: they are secrets.
