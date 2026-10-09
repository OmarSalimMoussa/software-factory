# Driving the remote desktop

Some steps happen outside the web UI, such as a browser extension. A product that has them offers a remote desktop
per slot: a real desktop whose view is a live video in a web page. A tester works it by sight, from its own browser:
a screenshot, then a click or a key at a point on the video.

Use it only for the steps that need it. One tester holds a slot's desktop at a time, for the desktop time its brief
gives in all, counted from its start.

## 1. Open it

1. `factory slot desktop up <address>` prints the view's address and a one-time password. A product without a
   desktop answers "not supported": report the step blocked.
2. Resize your browser so a screenshot is not shrunk before you see it; otherwise your clicks land short of where
   you aimed.
3. Open the view's address, enter the password, and take control as the view asks. Never put the password in your
   report.

## 2. Work it

- **Click:** take a screenshot, click at the point, then take a screenshot again to check what happened.
- **Type:** click the field on the video first, then type into the layer the view takes keys from.
- **One key,** such as Escape, right after a type.

## 3. Finish

Put your browser back to the first width your brief lists. Each `desktop up` gives new credentials; a change to what
runs on the desktop needs it run again.
