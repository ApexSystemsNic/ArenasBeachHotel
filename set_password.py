"""Change a staff account's password from this PC.

    py set_password.py            # list accounts still on a factory password
    py set_password.py admin      # set a new password for 'admin' (typed hidden)
"""
import getpass
import sys

import database as db


def main():
    if len(sys.argv) < 2:
        weak = db.users_with_factory_passwords()
        if weak:
            print('Estas cuentas todavía usan la contraseña de fábrica:', ', '.join(weak))
            print('Cámbielas con:  py set_password.py <usuario>')
        else:
            print('Ninguna cuenta usa una contraseña de fábrica.')
        return

    username = sys.argv[1]
    password = getpass.getpass(f'Nueva contraseña para {username} (mínimo {db.MIN_PASSWORD_LENGTH} caracteres): ')
    if password != getpass.getpass('Repítala: '):
        sys.exit('Las contraseñas no coinciden; no se cambió nada.')
    try:
        db.set_user_password(username, password)
    except ValueError as e:
        sys.exit(f'No se cambió: {e}')
    print(f'Contraseña de {username} actualizada.')


if __name__ == '__main__':
    main()
