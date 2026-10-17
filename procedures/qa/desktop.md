# Driving the remote desktop

Some steps happen outside the web UI, such as a browser extension. A product that has them offers a remote desktop
per slot: a real desktop whose view is a live video in a web page. A tester works it by sight, from its own browser:
a screenshot, then a click or a key at a point on the video.

Use it only for the steps that need it. One tester holds a slot's desktop at a time, and the machine runs only a few
desktops at once; each tester holds one for the desktop time its brief gives in all, counted from its start. The
time `desktop up` takes is not counted. When the factory refuses a desktop, test what needs none first, then try
again.

## 1. Open it

1. `factory slot desktop up <address>` prints the view's address and a one-time password. A product without a
   desktop answers "not supported": report the step blocked.
2. `browser_open_desktop` with the view's address. It sets your browser to the desktop's own size first, so a
   screenshot is not shrunk before you see it and a point in it is the point on the desktop.
3. Enter the password and take control as the view asks. Never put the password in your report.

## 2. Work it

- **Click:** take a screenshot, click at the point, then take a screenshot again to check what happened.
- **Type:** click the field on the video first, then type into the layer the view takes keys from.
- **One key,** such as Escape, right after a type.
- **A window taller than the desktop** hides its lower part: maximise it first, by double-clicking its title bar.
- **To replace an address or a field's text,** click it, select all with Ctrl+A, then type.

## 3. Finish

Put your browser back to the first width your brief lists. Each `desktop up` gives new credentials; a change to what
runs on the desktop needs it run again.
