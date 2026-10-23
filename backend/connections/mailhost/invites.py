"""The iCalendar for a mail-host invite: the emailed request and the copy stored on the organizer's calendar.

Both carry one UID.  The stored copy marks every attendee ``SCHEDULE-AGENT=CLIENT`` so a host with
implicit scheduling (iCloud) does not send a second invitation of its own.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage

from icalendar import Calendar, Event, vCalAddress

from connections.calendar_document import Invite

PRODID = "-//CORSIA//mail-host invite//EN"


def _calendar(sender: str, invite: Invite, uid: str, *, method: str | None) -> Calendar:
    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    if method:
        calendar.add("method", method)
    event = Event()
    event.add("uid", uid)
    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("summary", invite.title)
    event.add("dtstart", invite.start_at)
    event.add("dtend", invite.end_at)
    event.add("description", invite.description)
    event.add("organizer", f"mailto:{sender}")
    for address in invite.attendees:
        attendee = vCalAddress(f"mailto:{address}")
        if method is None:
            attendee.params["SCHEDULE-AGENT"] = "CLIENT"
        event.add("attendee", attendee, encode=False)
    calendar.add_component(event)
    return calendar


def stored_event(sender: str, invite: Invite, uid: str) -> str:
    """The event as the organizer's own calendar keeps it."""
    return _calendar(sender, invite, uid, method=None).to_ical().decode("utf-8")


def invite_message(sender: str, invite: Invite, uid: str) -> EmailMessage:
    """A standards-shaped iCalendar request email for the attendees."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(invite.attendees)
    message["Subject"] = f"Invitation: {invite.title}"
    message.set_content(invite.description or f"Invitation: {invite.title}")
    request = _calendar(sender, invite, uid, method="REQUEST").to_ical().decode("utf-8")
    message.add_alternative(request, subtype="calendar", params={"method": "REQUEST"})
    return message
