"""Send macOS system media key events via Quartz. Works with YouTube, Spotify, etc."""
import sys
import Quartz

# NX_KEYTYPE: 16=play/pause, 17=next, 18=previous
KEY_MAP = {"toggle": 16, "play": 16, "pause": 16, "next": 17, "prev": 18}


def send_media_key(key_type):
    for flag in (0xa00, 0xb00):  # key down, key up
        data1_flag = 0xa if flag == 0xa00 else 0xb
        ev = Quartz.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), flag, 0, 0, 0, 8,
            (key_type << 16) | (data1_flag << 8), -1
        )
        Quartz.CGEventPost(0, ev.CGEvent())


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "toggle"
    key_type = KEY_MAP.get(action)
    if key_type is None:
        print(f"Unknown action: {action}", file=sys.stderr)
        sys.exit(1)
    send_media_key(key_type)
