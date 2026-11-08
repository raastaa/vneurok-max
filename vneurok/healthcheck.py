"""Bounded HTTP probe of this container's loopback endpoint. Never calls MAX."""
import argparse
import http.client
import json
import os
from urllib.parse import urlsplit


class HealthError(Exception):
    pass


def check_health(*, port=8765, origin=None, allow_demo=False, timeout=3):
    if type(port) is not int or not 1 <= port <= 65535:
        raise HealthError('Неверный локальный порт.')
    origin = origin if origin is not None else os.getenv('APP_ORIGIN', f'http://127.0.0.1:{port}')
    try:
        parsed = urlsplit(origin)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or any(ord(c) < 33 or ord(c) == 127 for c in origin)):
            raise ValueError()
        parsed.port  # Reject malformed ports before constructing any headers.
        host = parsed.netloc
    except (ValueError, TypeError, AttributeError) as error:
        raise HealthError('APP_ORIGIN не подходит для проверки Host.') from error
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        # Direct HTTPConnection ignores proxy variables and never follows redirects.
        connection.request('GET', '/health', headers={'Host': host, 'Accept': 'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            raise HealthError('Локальный /health вернул неуспешный статус.')
        if response.getheader('Content-Type', '').split(';', 1)[0].strip() != 'application/json':
            raise HealthError('Локальный /health вернул неверный формат.')
        raw = response.read(8193)
        if len(raw) > 8192:
            raise HealthError('Ответ /health превышает ограничение.')
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get('ok') is not True or type(data.get('demo')) is not bool
                or not isinstance(data.get('version'), str) or not 1 <= len(data['version']) <= 64):
            raise HealthError('Ответ /health не соответствует ожидаемому протоколу.')
        if data['demo'] and not allow_demo:
            raise HealthError('Вместо школьного режима запущена демонстрация.')
        return {'ok': True, 'demo': data['demo'], 'version': data['version']}
    except (OSError, http.client.HTTPException, ValueError, UnicodeError) as error:
        raise HealthError('Нет корректного ответа локального сервиса.') from error
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Проверить только локальный HTTP /health, без MAX, токенов и редиректов.')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--allow-demo', action='store_true', help='Только для локальной smoke-проверки демонстрации.')
    args = parser.parse_args(argv)
    try:
        result = check_health(port=args.port, allow_demo=args.allow_demo)
    except HealthError as error:
        print(str(error))
        return 1
    print('Локальный /health готов: ' + ('демонстрация.' if result['demo'] else 'школьный режим.'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
