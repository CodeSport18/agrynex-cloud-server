# Agrynex Cloud Server

Connects the Agrynex Raspberry Pi on the farm to the mobile app and a browser dashboard. Built with Flask, Flask-SocketIO, MongoDB Atlas and Amazon S3, and hosted on Render.

- The **Pi** keeps a Socket.IO connection open to `/device`. It reports its state, receives settings and commands, and sends live video frames while someone is watching. It reports alerts over HTTP and uploads snapshots and clips straight to S3 using pre-signed links.
- The **mobile app** and **dashboard** use the JSON API under `/api` and a Socket.IO connection to `/client` for live status, new alerts and live video.

## What it does

- Alerts with a snapshot and a short clip. The clip starts a few seconds before the detection and runs a few seconds after it.
- Live view. The Pi streams only while someone has it open.
- Controls: system on/off (pauses detection and the camera), animal detection on/off, person detection on/off, siren on/off and sprinkler on/off. The detection switches stay in sync with the ones on the Pi's local web page, in both directions. When switched on by hand, the siren and sprinkler turn off automatically after a set maximum time.
- Settings: live view, clips and snapshots can each be turned on or off. You can also set clip length, how long the siren and sprinkler run after a detection, the manual safety cap, the minimum time between alerts, and how many days to keep alerts.
- Users: an admin adds people. There's no public sign-up. Everyone can view alerts and use the controls. Only admins can change settings and manage users.
- Alerts older than the retention period are deleted every hour, along with their S3 files.
- Push notifications aren't built yet. `app/notifications.py` is where they go.

## Setup

### 1. MongoDB Atlas

In Atlas, create a database user and copy the connection string (**Connect → Drivers**). Under **Network Access**, allow connections from Render. Render's outbound IPs are listed on the service's **Connect** tab, or you can use `0.0.0.0/0` together with a strong database password.

### 2. S3 bucket

Keep the bucket private. Create an IAM user with only this policy (replace `YOUR-BUCKET`) and create an access key for it:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
    "Resource": "arn:aws:s3:::YOUR-BUCKET/alerts/*"
  }]
}
```

`s3:DeleteObject` lets the server remove old alerts. With these permissions, the "is the file uploaded?" check returns 403 rather than 404 for missing files, which the server treats the same way.

### 3. Render

1. In Render, choose **New → Blueprint** and pick this repository. `render.yaml` sets up the service.
2. Fill in the variables Render asks for: `MONGODB_URI`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` and `S3_BUCKET`. Render generates `SECRET_KEY` and `DEVICE_TOKEN` for you.
3. After the first deploy, copy `DEVICE_TOKEN` from the service's **Environment** tab into the Pi's `.env` file.
4. Sign in at `https://<your-service>.onrender.com` with the admin email and password. The admin account is created only when the database has no users. After that, you can remove `ADMIN_PASSWORD` from Render.

The service uses the **Starter** plan because the free plan sleeps when idle, which would disconnect the Pi. It must run as **one instance with one worker**, because the link to the Pi is held in that process's memory.

## Local development

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # then fill it in; set SESSION_COOKIE_SECURE=false
python wsgi.py         # http://localhost:8000
```

To test without AWS, run a fake S3 with `moto_server -p 5001`, create a bucket in it, and set `S3_ENDPOINT_URL=http://localhost:5001`.

Run the tests (they use an in-memory MongoDB and a fake S3):

```bash
AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x python -m pytest
```

## API summary

The app sends `Authorization: Bearer <token>`, using the token from `/api/auth/login`. The dashboard uses its session cookie instead, and sends `X-Requested-With: fetch` on changes.

| Method | Path | Who |
|---|---|---|
| POST | `/api/auth/login` | anyone |
| GET | `/api/auth/me` | signed in |
| POST | `/api/auth/change-password` | signed in |
| GET | `/api/status` | signed in |
| POST | `/api/control` `{target: system\|animal_detection\|human_detection\|siren\|sprinkler, on}` | signed in |
| GET / PUT | `/api/settings` | GET signed in, PUT admin |
| GET | `/api/alerts?limit=&before=<alert id>` | signed in |
| GET / DELETE | `/api/alerts/<id>` | signed in |
| GET / POST | `/api/users` | admin |
| POST | `/api/users/<id>/password` | admin |
| DELETE | `/api/users/<id>` | admin |
| POST | `/api/device/alerts` | Pi (device token) |
| POST | `/api/device/alerts/<id>/media` | Pi (device token) |
