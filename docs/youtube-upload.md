# Setting up one-click YouTube upload

Every recording in Coach Potato already has a **📁 Upload to YouTube** button
that needs no setup: it shows the video in Explorer, copies its path and opens
youtube.com/upload. **Most people should just use that.**

One-click upload sends the video from inside the app instead, with the title
and the chapter-per-death description already filled in. It needs a free
Google Cloud "OAuth client" of your own, because the upload runs as you, on
your channel. Setup takes about ten minutes, once.

Before you start, know Google's limits for a personal project like this:

| | |
|---|---|
| Video privacy | **Always private** until Google audits the project (you can make it unlisted/public on YouTube afterwards) |
| Uploads per day | About **6** (each upload costs 1,600 of the default 10,000 daily quota units) |
| Sign-in lasts | **7 days** while the app is in *Testing* — see step 7 to remove that |

## 0. Requirement: the Google upload libraries

One-click upload needs Google's Python client libraries, which are **not part
of the downloadable Windows/macOS build**. If you run Coach Potato from source:

```bash
.venv/bin/pip install -r requirements-youtube.txt     # Windows: .venv\Scripts\pip ...
```

…then restart the app. Settings → Recordings → **Test configuration** tells
you whether they're available.

## 1. Create a Google Cloud project

Open <https://console.cloud.google.com/projectcreate>, sign in with any Google
account, name the project (e.g. `Coach Potato`) and click **Create**. Make sure
the new project is selected in the project picker at the top of the page for
every following step.

## 2. Enable the YouTube Data API v3

Open <https://console.cloud.google.com/apis/library/youtube.googleapis.com> and
click **Enable**.

## 3. Set up the consent screen

Open <https://console.cloud.google.com/auth/overview> → **Get started**:

- **App name**: `Coach Potato` (anything — only you will see it)
- **User support email**: your email
- **Audience**: **External**
- **Contact information**: your email
- Agree to the policy → **Create**

## 4. Add yourself as a test user

Open <https://console.cloud.google.com/auth/audience> → **Test users** →
**Add users**, and add the Google account **that owns your YouTube channel**.
Skipping this is the most common setup problem: sign-in then fails with
*"Access blocked: … has not completed the Google verification process"*.

## 5. Create a Desktop app client and download its JSON

Open <https://console.cloud.google.com/auth/clients> → **Create client**:

- **Application type**: **Desktop app** (not *Web application*)
- **Name**: anything

Click **Create**, then **Download JSON**. Move the file somewhere permanent
(e.g. `Documents\CoachPotato\youtube-client.json`) — the app reads it from that
path every time it needs to sign in.

## 6. Point Coach Potato at it and test

Settings → **Recordings** → *Uploading to YouTube*:

1. Paste the JSON file's full path into **Client secrets (JSON)**. On Windows,
   Shift+right-click the file → **Copy as path** (the quotes it adds are fine).
2. Click **Test configuration**. Every row should be green except *Google
   sign-in*, which says you haven't signed in yet.
3. Click **Sign in with Google**. A browser tab opens: choose the account from
   step 4. Google warns that it **hasn't verified this app** — it's your own
   app, so click **Continue**, then allow the upload permission.
4. Test again — all green means the next upload goes straight through.
5. Click **Save**.

## 7. Optional: stop the weekly re-sign-in

While the app's publishing status is *Testing*, Google expires the sign-in
after 7 days, and the next upload (or Test configuration) asks you to sign in
again. To avoid that, open <https://console.cloud.google.com/auth/audience> and
click **Publish app**. You don't need to submit it for verification: you'll
just keep seeing the "unverified app" warning when signing in, which is
expected for a personal project.

## Troubleshooting

**Test configuration** names each problem and how to fix it. The common ones:

| Message | Fix |
|---|---|
| This is a Web application client | Create a new client with type **Desktop app** (step 5) |
| Not enabled for this Google Cloud project | Enable the API (step 2) in the **same** project as the client |
| Google refused the sign-in (access_denied) | Add your account as a test user (step 4) |
| The saved sign-in no longer works | Sign in again; see step 7 to stop it expiring |
| Today's quota is used up | Wait until midnight Pacific time, or use the 📁 manual upload |
