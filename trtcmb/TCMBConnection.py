import datetime

import frappe
import requests

from trtcmb.TCMBCurrency import TCMBCurrency
from trtcmb.TCMBCurrencyExchange import TCMBCurrencyExchange


class TCMBConnection:

    def __init__(self):
        self.a_day = datetime.timedelta(days=1)
        self.series_separator = "-"
        self.inner_separator = "."
        self.series_prefix = "/series="
        self.start_date_prefix = "&startDate="
        self.end_date_prefix = "&endDate="
        self.datagroup_code = "bie_dkdovizgn"
        # global settings
        self.company = frappe.defaults.get_user_default(TCMBCurrency.company_doctype)
        # company settings
        self.enable = frappe.db.get_value(TCMBCurrency.company_setting_doctype, self.company, "enable")
        self.key = frappe.db.get_value(TCMBCurrency.company_setting_doctype, self.company, "key")
        self.start_date = frappe.db.get_value(TCMBCurrency.company_setting_doctype, self.company, "start_date")
        self.last_updated = frappe.db.get_value(TCMBCurrency.company_setting_doctype, self.company, "last_updated")
        self.enable_update = frappe.db.get_value(TCMBCurrency.company_setting_doctype, self.company, "enable_update")

    def get_exchange_rates_for_enabled_currencies(self, datagroup_code: str):
        if datagroup_code != self.datagroup_code or self.enable != 1:
            # should be error
            return False
        currency_list = TCMBCurrency.get_list_of_enabled_currencies()
        
        # Determine effective start date
        # 1. Start with global setting or fallback
        tcmb_start_date = datetime.date.today()
        if self.start_date is not None and self.start_date > datetime.date(1950, 1, 2):
            tcmb_start_date = self.start_date
            
        # 2. Check for the last successful exchange rate in DB to avoid re-fetching old data
        last_rate_date = frappe.db.sql("""
            SELECT MAX(date) FROM `tabCurrency Exchange`
            WHERE to_currency = 'TRY'
        """)
        
        if last_rate_date and last_rate_date[0][0]:
            # If we have data, resume from the day AFTER the last record
            last_date = last_rate_date[0][0]
            if isinstance(last_date, str):
                last_date = datetime.datetime.strptime(last_date, "%Y-%m-%d").date()
            tcmb_start_date = last_date + datetime.timedelta(days=1)
        
        target_end_date = datetime.date.today()
        
        # Security check: Don't go into the future
        if tcmb_start_date > target_end_date:
            return target_end_date

        # Chunking Logic: Process in 15-day chunks to prevent API timeouts
        chunk_size_days = 15
        current_chunk_start = tcmb_start_date
        
        while current_chunk_start <= target_end_date:
            current_chunk_end = current_chunk_start + datetime.timedelta(days=chunk_size_days)
            if current_chunk_end > target_end_date:
                current_chunk_end = target_end_date
            
            # Fetch for this chunk
            tcmb_exchange_rates = self.get_exchange_rates(currency_list=currency_list,
                                                          from_date=current_chunk_start,
                                                          to_date=current_chunk_end)
            
            for tcmb_exchange_rate_data in tcmb_exchange_rates:
                TCMBCurrencyExchange.commit_single_currency_exchange_rate(tcmb_exchange_rate_data, self.enable_update)
            
            # Move to next chunk
            current_chunk_start = current_chunk_end + datetime.timedelta(days=1)

        return datetime.datetime.today().date()

    def connect(self, datagroup_code: str, series_list: list, for_start_date: datetime.date,
                for_end_date: datetime.date):
        if datagroup_code != self.datagroup_code or self.enable != 1:
            # should be error
            return False
        # Exchange, rates, Daily, (Converted, to, TRY)
        series = self.series_prefix + self.series_separator.join(series_list)
        tcmb_start_date = self.start_date_prefix + for_start_date.strftime(TCMBCurrencyExchange.tcmb_date_format)
        tcmb_end_date = self.end_date_prefix + for_end_date.strftime(TCMBCurrencyExchange.tcmb_date_format)
        return_type = TCMBCurrency.type_prefix + TCMBCurrency.response_type
        url = TCMBCurrency.service_path + series + tcmb_start_date + tcmb_end_date + return_type
        return requests.get(url, headers={'key': self.key}).json()

    def get_single_exchange_rate(self, currency: str, for_date: datetime.date, purpose: str, search_forward: bool = False):
        # dummy assignment
        currency_series_data = ""
        if purpose == "for_buying":
            currency_series_data = self.inner_separator.join(
                ["TP", "DK", currency, TCMBCurrencyExchange.buying_code])
        elif purpose == "for_selling":
            currency_series_data = self.inner_separator.join(
                ["TP", "DK", currency, TCMBCurrencyExchange.selling_code])
        series_as_list = [currency_series_data]
        # Exchange, rates, Daily, (Converted, to, TRY)
        response_dict = self.connect(datagroup_code=self.datagroup_code, series_list=series_as_list,
                                     for_start_date=for_date, for_end_date=for_date)
        if response_dict.get("totalCount") == 1:
            currency_response = currency_series_data.replace(self.inner_separator,
                                                             TCMBCurrencyExchange.response_separator)
            if response_dict.get("items")[0].get(currency_response) is None:
                # Check for weekend or forced forward search
                if search_forward or for_date.weekday() >= 5: # Sat or Sun
                     exchange_rate_date = datetime.datetime.strptime(response_dict.get("items")[0].get("Tarih"),
                                                                TCMBCurrencyExchange.tcmb_date_format).date() + \
                                     self.a_day
                     search_forward = True
                else:
                     exchange_rate_date = datetime.datetime.strptime(response_dict.get("items")[0].get("Tarih"),
                                                                TCMBCurrencyExchange.tcmb_date_format).date() - \
                                     self.a_day
                     
                new_dict = self.get_single_exchange_rate(currency, exchange_rate_date, purpose, search_forward=search_forward)
                response_dict["items"][0][currency_response] = new_dict["items"][0][currency_response]
        return response_dict

    def get_exchange_rates(self, currency_list: list, from_date: datetime.date, to_date: datetime.date):
        # dummy assignment
        currency_series_as_list = list()
        for currency in currency_list:
            currency_series_as_list.append(self.inner_separator.join(
                ["TP", "DK", currency.get("currency_name"), TCMBCurrencyExchange.buying_code]))
            currency_series_as_list.append(self.inner_separator.join(
                ["TP", "DK", currency.get("currency_name"), TCMBCurrencyExchange.selling_code]))
        # Exchange, rates, Daily, (Converted, to, TRY)
        response_dict = self.connect(datagroup_code=self.datagroup_code, series_list=currency_series_as_list,
                                     for_start_date=from_date, for_end_date=to_date)
        return_list = list()
        if response_dict.get("totalCount") >= 1:
            currency_series_data = response_dict.pop("items")
            reference_dict = dict()
            for currency_tuple in currency_series_data:
                currency_tuple.pop(TCMBCurrencyExchange.tcmb_strip_key)
                reference_date = currency_tuple.pop("Tarih")
                for tuple_key in list(currency_tuple):
                    reference_dict[reference_date + tuple_key] = currency_tuple.get(tuple_key)
                    if currency_tuple.get(tuple_key) is None:
                        current_date_obj = datetime.datetime.strptime(reference_date, TCMBCurrencyExchange.tcmb_date_format).date()
                        search_forward = False
                        if current_date_obj.weekday() >= 5: # Sat or Sun
                             exchange_rate_date = current_date_obj + self.a_day
                             search_forward = True
                        else:
                             exchange_rate_date = current_date_obj - self.a_day

                        # tcmb_series_split = str(tcmb_series).split(".")
                        tcmb_series_split = str(tuple_key).split("_")
                        # TODO: Check later if purpose is blank or not
                        purpose = ""
                        if tcmb_series_split[3] == TCMBCurrencyExchange.buying_code:
                            purpose = "for_buying"
                        if tcmb_series_split[3] == TCMBCurrencyExchange.selling_code:
                            purpose = "for_selling"
                        if reference_dict.get(
                                datetime.datetime.strftime(exchange_rate_date, '%d-%m-%Y') + tuple_key) is None:
                            new_dict = self.get_single_exchange_rate(tcmb_series_split[2], exchange_rate_date,
                                                                     purpose=purpose, search_forward=search_forward)
                            currency_tuple[tuple_key] = new_dict["items"][0][tuple_key]
                        else:
                            currency_tuple[tuple_key] = reference_dict.get(
                                datetime.datetime.strftime(exchange_rate_date, '%d-%m-%Y') + tuple_key)
                currency_tuple[TCMBCurrencyExchange.tcmb_date_key] = reference_date
                return_list.append(currency_tuple)
        return return_list
