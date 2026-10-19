# Driving the remote desktop

Some steps happen outside the web UI, such as a browser extension. A product that has them offers a remote desktop
per slot: a real desktop whose view is a live video in a web page. A tester works it by sight, from its own browser:
a screenshot, then a click or a key at a point on the video.

Use it only for the steps that need it. One tester holds a slot's desktop at a time, and the machine runs only a few
desktops at once. Each tester holds one for the seconds its `desktop up` answers with (`desktop_seconds`), whatever a
brief or the product's own notes say, counted from its first `desktop up`. While you hold it, that is your time,
however long an item gets. The time `desktop up` takes is not counted, nor its wait for a desktop another tester
holds. When the factory still refuses one, test what needs none first, then try again.

## 1. Open it

1. `factory slot desktop up <test address>`, the email address your scenario signs in as, prints the view's web
   address and a one-time password. It may take minutes, on the factory's time, not yours: never skip it for lack of
   time. A product without a desktop answers "not supported": report the step blocked.
2. `browser_open_desktop` with the view's web address. It sets your browser to the desktop's own size first, so a
   screenshot is not shrunk before you see it and a point in it is the point on the desktop.
3. Enter the password and take control as the view asks. Never put the password in your report.

## 2. Work it

- **Click:** take a screenshot, click at the point with `browser_mouse_press_xy`, which holds the button as the desktop
  needs, then take a screenshot again to check what happened. Take each point from a screenshot taken after the last
  change: a panel that opens or closes moves what is under it.
- **Check by screenshot.** The desktop's words are pixels in a video, not page text: a text wait or a snapshot never
  sees them.
- **Type:** click where the text goes on the video first, then `browser_type_keys` with the text: one call types
  it all, key by key, capitals and symbols with Shift held, as the desktop takes it. The desktop may draw the keys
  seconds later: find them in a screenshot before you type again, or they come twice.
- **One key,** such as Escape, right after a type.
- **A window taller than the desktop** hides its lower part: maximise it first, by double-clicking its title bar.
- **To replace an address or a field's text,** click it, select all with Ctrl+A, then type.
- **To scroll a page,** click a blank part of it, then press PageDown, PageUp or the arrow keys: a mouse wheel on the
  live view may not reach the desktop.

## 3. Finish

Put your browser back to the first width your brief lists. The desktop stays up until you hand back, and the factory
takes it down then, whatever the product's notes say: never run `desktop down`. Each `desktop up` gives new
credentials; a change to what runs on the desktop needs it run again.
