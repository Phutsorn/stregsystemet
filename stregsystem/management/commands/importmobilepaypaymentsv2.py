from datetime import datetime, timedelta, date
from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_datetime
from pathlib import Path

from requests import HTTPError
from base64 import b64encode

from stregsystem.models import MobilePayment
import json
import logging
import requests

from stregsystem.utils import mobile_payment_exact_match_member, strip_emoji


class Command(BaseCommand):
    help = 'Imports the latest payments from MobilePay'

    api_endpoint = 'https://api.vipps.no/'
    # Saves secret tokens to the file "tokens.json" right next to this file.
    # Important to use a separate file since the tokens can change and is thus not suitable for django settings.
    tokens_file = (Path(__file__).parent / 'tokens.json').as_posix()
    tokens_file_backup = (Path(__file__).parent / 'tokens.json.bak').as_posix()
    tokens = None
    manual_cutoff_date = date(2024, 4, 9)
    myshop_number = 90601

    logger = logging.getLogger(__name__)
    days_back = None

    def add_arguments(self, parser):
        parser.add_argument(
            'days_back',
            nargs='?',
            type=int,
            default=7,
            help="Days back from today to look for MobilePay transactions (max 31 days)",
        )

    def handle(self, *args, **options):
        self.days_back = options['days_back'] if options['days_back'] <= 31 else 7
        self.import_mobilepay_payments()

    def write_debug(self, s):
        self.logger.debug(s)

    def write_info(self, s):
        self.logger.info(s)

    def write_warning(self, s):
        self.logger.warning(s)

    def write_error(self, s):
        self.logger.error(s)

    # Reads the token file from disk
    def read_token_storage(self):
        with open(self.tokens_file, 'r') as json_file:
            self.tokens = json.load(json_file)

        if self.tokens is None:
            self.write_error("read token from storage. 'tokens' is None. Reverting to backup tokens")

            with open(self.tokens_file_backup, 'r') as json_file_backup:
                self.tokens = json.load(json_file_backup)

    # Saves the token variable to disk
    def update_token_storage(self):
        if self.tokens is None:
            self.write_error(f"'tokens' is None. Aborted writing.")
            return

        with open(self.tokens_file, 'w') as json_file:
            json.dump(self.tokens, json_file, indent=2)

    # Fetches a new access token using the refresh token.
    def refresh_access_token(self):
        url = f"{self.api_endpoint}/miami/v1/token"

        payload = {
            "grant_type": "client_credentials",
        }

        authorization_string = '{}:{}'.format(self.tokens['client_id'], self.tokens['client_secret'])
        authorization_base64 = b64encode(authorization_string.encode("ascii")).decode("ascii")
        headers = {
            'authorization': 'Basic {}'.format(authorization_base64),
        }

        response = requests.post(url, data=payload, headers=headers)
        response.raise_for_status()
        json_response = response.json()
        # Calculate when the token expires
        expire_time = datetime.now() + timedelta(seconds=json_response['expires_in'] - 1)
        self.tokens['access_token_timeout'] = expire_time.isoformat(timespec='milliseconds')
        self.tokens['access_token'] = json_response['access_token']
        self.update_token_storage()

    # Fetches the transactions for a given payment-point (MobilePay phone-number) in a given period (from-to)
    def get_transactions(self, date: date):
        # {self.tokens['paymentpoint']}

        topic = "funds"
        ledgerDate = date.strftime('%Y-%m-%d')

        url = f"{self.api_endpoint}/report/v2/ledgers/{self.ledgerId}/{topic}/dates/{ledgerDate}"

        params = {
            'includeGDPRSensitiveData': "true",
        }
        headers = {
            'authorization': 'Bearer {}'.format(self.tokens['access_token']),
        }
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()['items']

    # Client side check if the token has expired.
    def refresh_expired_token(self):
        self.read_token_storage()
        expire_time = parse_datetime(self.tokens['access_token_timeout'])
        if datetime.now() >= expire_time:
            self.refresh_access_token()

    def fetch_transactions(self):
        # Do a client side check if token is good. If not - fetch another token.
        try:
            self.refresh_expired_token()
            assert self.days_back is not None

            transactions = []

            for i in range(self.days_back):
                past_date = datetime.now() - timedelta(days=i)
                if past_date < self.manual_cutoff_date:
                    break

                transactions.extend(self.get_transactions(past_date))

            return transactions
        except HTTPError as e:
            self.write_error(f"Got an HTTP error when trying to fetch transactions: {e.response}")
        except Exception as e:
            self.write_error(f'Got an error when trying to fetch transactions.')
            pass

    def import_mobilepay_payments(self):
        transactions = self.fetch_transactions()
        if transactions is None:
            self.write_info(f'Ran, but no transactions found')
            return

        for transaction in transactions:
            self.import_mobilepay_payment(transaction)

        self.write_info('Successfully ran MobilePayment API import')

    def import_mobilepay_payment(self, transaction):
        """
        Example of a transaction:
        {
            "pspReference": "32212390715",
            "time": "2024-04-05T07:19:26.528092Z",
            "ledgerDate": "2024-04-05",
            "entryType": "capture",
            "reference": "10113143347",
            "currency": "DKK",
            "amount": 20000,
            "recipientHandle": "DK:90601",
            "balanceAfter": 110000,
            "balanceBefore": 90000,
            "name": "Jakob Topper",
            "maskedPhoneNo": "xxxx 1234",
            "message": "Topper"
        }
        :param transaction:
        :return:
        """
        if transaction['entryType'] != 'capture':
            return

        trans_id = transaction['pspReference']

        if MobilePayment.objects.filter(transaction_id=trans_id).exists():
            self.write_debug(f'Skipping transaction since it already exists (PSP-Reference: {trans_id})')
            return

        currency_code = transaction['currency']
        if currency_code != 'DKK':
            self.write_warning(f'Does ONLY support DKK (Transaction ID: {trans_id}), was {currency_code}')
            return

        amount = transaction['amount']

        comment = strip_emoji(transaction['message'])

        payment_datetime = parse_datetime(transaction['time'])

        MobilePayment.objects.create(
            amount=amount,  # already in streg-ører
            member=mobile_payment_exact_match_member(comment),
            comment=comment,
            timestamp=payment_datetime,
            transaction_id=trans_id,
            status=MobilePayment.UNSET,
        )

        self.write_info(f'Imported transaction id: {trans_id} for amount: {amount}')

    def get_ledger_info(self, myshop_number: int):
        """
        {
            "ledgerId": "123456",
            "currency": "DKK",
            "payoutBankAccount": {
                "scheme": "BBAN:DK",
                "id": "123412341234123412"
            },
            "owner": {
                "scheme": "business:DK:CVR",
                "id": "16427888"
            },
            "settlesForRecipientHandles": [
                "DK:90601"
            ]
        }
        :param myshop_number:
        :return:
        """
        url = f"{self.api_endpoint}/settlement/v1/ledgers"
        params = {'settlesForRecipientHandles': 'DK:{}'.format(myshop_number)}
        headers = {
            'authorization': 'Bearer {}'.format(self.tokens['access_token']),
        }
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()

        ledger_info = response.json()["items"]
        # TODO: handle no ledgers returned

        return ledger_info[0]

    def get_ledger_id(self, myshop_number: int) -> int:
        return int(self.get_ledger_info(myshop_number)["ledgerId"])
