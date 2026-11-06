"""What the rate includes, the restaurant/bar hours and the hotel's policies —
one source for the printed confirmation, the agency rooming list and their
Word (.docx) export, so the texts never drift apart."""

RATE_INCLUDES = [
    'Desayuno a la carta a partir del siguiente día a la noche de llegada',
    'Traslados aeropuerto de Corn Island – hotel – aeropuerto de Corn Island',
]

HOURS = [
    'Horario de Restaurante: 6:15 AM – 9:00 PM',
    'Horario de Bar: 6:15 AM – 10:00 PM',
]

GENERAL = [
    'Toda persona que entre al hotel en calidad de huésped, tiene la obligación de registrarse en recepción por cada habitación.',
    'Toda persona titular o a cargo de la habitación tiene la obligación de dejar un voucher abierto o un depósito a su llegada en recepción, por cualquier desperfecto que cause o cargo adicional que se realice, está obligado a cubrir el costo.',
    'La hora de entrega de la habitación en el hotel (CHECK IN) es a las 2:00 P.M.',
    'La hora de la salida de la habitación en el hotel (CHECK OUT) es a las 12:00 P.M. En caso de hacer un CHECK OUT posterior al acordado se cobrará una tarifa de C$550.00 córdobas adicionales por hora, a menos que notifique a recepción y sea autorizado a prolongar su estancia sin costo.',
    'Reservaciones son prepagadas en su totalidad.',
    'El hotel no acepta visitas adicionales a las habitaciones que ocupen nuestros huéspedes, siendo derecho del hotel poder solicitar la desocupación inmediata de habitaciones de las personas no autorizadas en la tarjeta de registro.',
    'En caso de escándalos o actos que pongan en riesgo la integridad de los demás huéspedes, ya sea en el interior de las habitaciones o áreas de uso común propiedad del hotel, se solicitará desalojar inmediatamente las habitaciones y cubrir el costo total de la habitación.',
    'Los daños causados a las instalaciones a causa del mal uso por parte del huésped serán cargados a la cuenta del mismo. Como son: extravío o daño a la llave de la habitación, control del televisor u objetos que se encuentren dentro de las habitaciones como son toallas, sábanas, decoración, etc.',
    'En caso de cancelación o salida anticipada se deberá cubrir el costo de la primera noche de tarifa RACK.',
    'La gratificación o propina para el personal de limpieza y servicio del establecimiento queda a criterio del huésped.',
    'El hotel no se hace responsable de cualquier robo y daño parcial o total de sus posesiones dentro de la habitación, instalaciones del hotel y/o automóvil.',
    'El hotel no se hace responsable del costo por cualquier accidente o percance dentro de sus instalaciones.',
    'No es permitido fumar en las instalaciones del hotel y restaurante. En caso de hacerlo, tiene un cargo adicional.',
    'No es permitido el ingreso de termos en las áreas del hotel y áreas públicas.',
]
GENERAL_ACCEPTANCE = ('Bajo este contrato ACEPTO las políticas y reglamentos del establecimiento en el cual me '
                      'hospedo, haciéndonos responsables de nuestros actos.')

RESERVATION = [
    'Las tarifas mostradas en el sitio Web son válidas únicamente al momento de su cotización, pueden cambiar sin previo aviso de acuerdo a las condiciones mientras no hayan sido reservadas y confirmadas con un pago total.',
    'Al momento de reservar autoriza de manera escrita y/o verbal a usar su número de tarjeta de crédito o el medio elegido para realizar su pago, y confirma expresamente conocer y aceptar nuestras políticas de reservación, de pago, de deslinde de responsabilidad y cancelación.',
    'Cualquier cambio de reservación está sujeto a disponibilidad, a una re-cotización de tarifas y en ciertos casos a un cargo de servicio por cambios. En caso de cancelación le pedimos leer nuestras POLÍTICAS DE CANCELACIÓN; el monto total de las reservaciones no es reembolsable.',
]


def cancellation(hotel_email):
    return [
        f'Cualquier cambio o cancelación a su reservación deberá solicitarla al (505) 8851 5046 (WhatsApp) o al {hotel_email}; en caso de hacerlo vía agencia de viajes, notificarles por la misma vía, con anticipación mínima de 72 horas antes de la fecha de llegada al hotel, proporcionando su(s) clave(s) de confirmación o nombre de la reserva.',
        'En caso de no presentarse por alguna razón sin previa notificación se hará el cargo de "No Show", que corresponde al cobro por el monto de una noche más impuestos por cada habitación.',
        'Independientemente de las noches reservadas, de no realizarse el cambio o cancelación en el tiempo y forma antes mencionada, o en caso de no presentarse el huésped en la fecha de entrada, se hará el cargo de "No Show" en su totalidad.',
        'En estancias de más de una noche, si el huésped no se presenta al hotel en la fecha y hora establecidas para el registro del check in, se aplicará el cargo de "No Show" solo por la primera noche de habitación reservada más impuestos y el resto de la estancia se cancelará automáticamente.',
        'La capacidad máxima de personas dependerá del tipo de habitación reservado.',
    ]


RESTRICTIONS = ('NO SE PERMITE INGRESO DE ALIMENTOS Y LICORES A LAS INSTALACIONES DEL HOTEL (en caso de '
                'ingresarlos tiene un costo de descorche por botella o por alimentos).')
