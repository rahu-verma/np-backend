from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings
import responses

from campaign.models import (
    Campaign,
    CampaignEmployee,
    DeliveryLocationEnum,
    Employee,
    EmployeeGroup,
    EmployeeGroupCampaign,
    EmployeeGroupCampaignProduct,
    Order,
    OrderProduct,
    Organization,
)
from inventory.models import Brand, Product, Supplier
from logistics.models import (
    LogisticsCenterEnum,
    LogisticsCenterInboundReceipt,
    LogisticsCenterInboundReceiptLine,
    LogisticsCenterMessage,
    LogisticsCenterMessageTypeEnum,
    LogisticsCenterOrderStatus,
    PurchaseOrder,
    PurchaseOrderProduct,
)
from logistics.providers.orian import (
    _platform_id_to_orian_id,
    add_or_update_inbound,
    add_or_update_outbound,
    add_or_update_product,
    add_or_update_supplier,
)
from logistics.tasks import (
    process_logistics_center_message,
    send_order_to_logistics_center,
    sync_product_with_logistics_center,
)
from services.address import format_street_line


@override_settings(
    ORIAN_BASE_URL='https://test.local',
    ORIAN_API_TOKEN='1234',
    ORIAN_CONSIGNEE='AAA',
)
class OrianProviderTestCase(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            name='supplier name',
        )
        self.brand = Brand.objects.create(
            name='brand name',
        )
        self.product_1 = Product.objects.create(
            brand=self.brand,
            supplier=self.supplier,
            name='product 1 name',
            sku='1',
            cost_price=50,
            sale_price=60,
        )
        self.product_2 = Product.objects.create(
            brand=self.brand,
            supplier=self.supplier,
            name='product 2 name',
            sku='2',
            cost_price=70,
            sale_price=80,
        )
        self.purchase_order_1 = PurchaseOrder.objects.create(supplier=self.supplier)
        PurchaseOrderProduct.objects.create(
            product_id=self.product_1,
            purchase_order=self.purchase_order_1,
            quantity_ordered=1,
            quantity_sent_to_logistics_center=0,
        )
        self.purchase_order_2 = PurchaseOrder.objects.create(supplier=self.supplier)
        PurchaseOrderProduct.objects.create(
            product_id=self.product_1,
            purchase_order=self.purchase_order_2,
            quantity_ordered=2,
            quantity_sent_to_logistics_center=0,
        )
        PurchaseOrderProduct.objects.create(
            product_id=self.product_2,
            purchase_order=self.purchase_order_2,
            quantity_ordered=3,
            quantity_sent_to_logistics_center=0,
        )

        # create the campaign infrastructure for the orders we need
        self.organization = Organization.objects.create(
            name='Test organization',
            manager_full_name='Test manager',
            manager_phone_number='0500000009',
            manager_email='manager@test.test',
        )
        campaign = Campaign.objects.create(
            name='Test campaign',
            organization=self.organization,
            status=Campaign.CampaignStatusEnum.ACTIVE.name,
            start_date_time=datetime.now(),
            end_date_time=datetime.now(),
        )
        self.employee_group_1 = EmployeeGroup.objects.create(
            name='Test employee group 1',
            organization=self.organization,
            delivery_city='Office1',
            delivery_street='Office street 1',
            delivery_street_number='1',
            delivery_apartment_number='2',
            delivery_location=DeliveryLocationEnum.ToHome.name,
        )
        self.employee_group_2 = EmployeeGroup.objects.create(
            name='Test employee group 2',
            organization=self.organization,
            delivery_city='Office2',
            delivery_street='Office street 2',
            delivery_street_number='3',
            delivery_apartment_number='4',
            delivery_location=DeliveryLocationEnum.ToOffice.name,
        )
        employee_group_campaign_1 = EmployeeGroupCampaign.objects.create(
            employee_group=self.employee_group_1,
            campaign=campaign,
            budget_per_employee=100,
        )
        self.employee_group_campaign_2 = EmployeeGroupCampaign.objects.create(
            employee_group=self.employee_group_2,
            campaign=campaign,
            budget_per_employee=100,
        )
        self.employee_1 = Employee.objects.create(
            employee_group=self.employee_group_1,
            first_name='Test',
            last_name='Employee 1',
            email='test1@test.test',
        )
        self.employee_2 = Employee.objects.create(
            employee_group=self.employee_group_2,
            first_name='Test',
            last_name='Employee 2',
            email='test2@test.test',
        )
        employee_group_1_campaign_product_1 = (
            EmployeeGroupCampaignProduct.objects.create(
                employee_group_campaign_id=employee_group_campaign_1,
                product_id=self.product_1,
            )
        )
        employee_group_1_campaign_product_2 = (
            EmployeeGroupCampaignProduct.objects.create(
                employee_group_campaign_id=employee_group_campaign_1,
                product_id=self.product_2,
            )
        )
        employee_group_2_campaign_product_1 = (
            EmployeeGroupCampaignProduct.objects.create(
                employee_group_campaign_id=self.employee_group_campaign_2,
                product_id=self.product_1,
            )
        )

        # the orders we need for testing outbound
        self.order_1 = Order.objects.create(
            campaign_employee_id=CampaignEmployee.objects.get(
                campaign=campaign, employee=self.employee_1
            ),
            order_date_time=datetime.now(),
            cost_from_budget=100,
            cost_added=0,
            status=Order.OrderStatusEnum.PENDING.name,
            full_name='Test name 1',
            phone_number='0500000000',
            additional_phone_number='050000001',
            delivery_city='City1',
            delivery_street='Main1',
            delivery_street_number='1',
            delivery_apartment_number='1',
            delivery_additional_details='Additional 1',
        )
        OrderProduct.objects.create(
            order_id=self.order_1,
            product_id=employee_group_1_campaign_product_1,
            quantity=1,
        )
        self.order_2 = Order.objects.create(
            campaign_employee_id=CampaignEmployee.objects.get(
                campaign=campaign, employee=self.employee_1
            ),
            order_date_time=datetime.now(),
            cost_from_budget=100,
            cost_added=0,
            status=Order.OrderStatusEnum.PENDING.name,
            full_name='Test name 2',
            phone_number='0500000002',
            additional_phone_number='050000003',
            delivery_city='City2',
            delivery_street='Main2',
            delivery_street_number='2',
            delivery_apartment_number='2',
            delivery_additional_details='Additional 2',
        )
        OrderProduct.objects.create(
            order_id=self.order_2,
            product_id=employee_group_1_campaign_product_1,
            quantity=2,
        )
        OrderProduct.objects.create(
            order_id=self.order_2,
            product_id=employee_group_1_campaign_product_2,
            quantity=3,
        )
        self.order_3 = Order.objects.create(
            campaign_employee_id=CampaignEmployee.objects.get(
                campaign=campaign, employee=self.employee_2
            ),
            order_date_time=datetime.now(),
            cost_from_budget=100,
            cost_added=0,
            status=Order.OrderStatusEnum.PENDING.name,
            full_name='Test name 3',
            phone_number='0500000004',
            additional_phone_number='050000005',
            delivery_city='City3',
            delivery_street='Main3',
            delivery_street_number='3',
            delivery_apartment_number='3',
            delivery_additional_details='Additional 3',
        )
        OrderProduct.objects.create(
            order_id=self.order_3,
            product_id=employee_group_2_campaign_product_1,
            quantity=4,
        )

    @responses.activate
    def test_add_or_update_supplier_failure(self):
        responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Company',
            json={
                'status': None,
                'MessageID': None,
                'Note': None,
                'errorCode': 'InvalidFormatData',
                'ErrorMessage': '',
            },
            status=200,  # orian responds with status 200 even with errors
        )

        result = add_or_update_supplier(self.supplier)

        # result should be false since the mock api responded with an error
        self.assertEquals(result, False)

    @responses.activate
    def test_add_or_update_supplier_success(self):
        responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Company',
            json={
                'status': 'SUCCSESS',
                'MessageID': None,
                'Note': 'Company Created/Updated',
                'errorCode': None,
                'ErrorMessage': None,
            },
            status=200,  # orian responds with status 200 even with errors
        )

        result = add_or_update_supplier(self.supplier)

        # result should be true since we succeeded
        self.assertEquals(result, True)

    @responses.activate
    def test_add_or_update_product_failure(self):
        responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Sku',
            json={
                'status': None,
                'MessageID': None,
                'Note': None,
                'errorCode': 'InvalidFormatData',
                'ErrorMessage': '',
            },
            status=200,  # orian responds with status 200 even with errors
        )

        result = add_or_update_product(self.product_1)

        # result should be false since the mock api responded with an error
        self.assertEquals(result, False)

    @responses.activate
    def test_add_or_update_product_success(self):
        responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Sku',
            json={
                'status': 'SUCCSESS',
                'MessageID': None,
                'Note': 'Sku Importer sent',
                'errorCode': None,
                'ErrorMessage': None,
            },
            status=200,  # orian responds with status 200 even with errors
        )

        result = add_or_update_product(self.product_1)

        # result should be true since we succeeded
        self.assertEquals(result, True)

    @responses.activate
    def test_add_or_update_inbound_failure(self):
        responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Inbound',
            json={
                'status': None,
                'MessageID': None,
                'Note': None,
                'errorCode': 'InvalidFormatData',
                'ErrorMessage': '',
            },
            status=200,  # orian responds with status 200 even with errors
        )

        result = add_or_update_inbound(self.purchase_order_1, datetime.now())

        # result should be false since the mock api responded with an error
        self.assertEquals(result, False)

    @responses.activate
    def test_add_or_update_inbound_success(self):
        responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Inbound',
            json={
                'status': 'SUCCSESS',
                'MessageID': None,
                'Note': 'Inbound Created/Updated',
                'errorCode': None,
                'ErrorMessage': None,
            },
            status=200,  # orian responds with status 200 even with errors
        )

        result = add_or_update_inbound(self.purchase_order_1, datetime.now())

        # result should be true since we succeeded
        self.assertEquals(result, True)

        # check that the body sent to the api contains the single product
        api_request_lines = json.loads(responses.calls[0].request.body)[
            'DATACOLLECTION'
        ]['DATA']['LINES']['LINE']
        self.assertEquals(len(api_request_lines), 1)
        self.assertEquals(api_request_lines[0]['SKU'], self.product_1.sku)

    @responses.activate
    def test_add_or_update_inbound_success_multiple_products(self):
        responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Inbound',
            json={
                'status': 'SUCCSESS',
                'MessageID': None,
                'Note': 'Inbound Created/Updated',
                'errorCode': None,
                'ErrorMessage': None,
            },
            status=200,  # orian responds with status 200 even with errors
        )

        result = add_or_update_inbound(self.purchase_order_2, datetime.now())

        # result should be true since we succeeded
        self.assertEquals(result, True)

        # check that the body sent to the api contains the two products
        api_request_lines = json.loads(responses.calls[0].request.body)[
            'DATACOLLECTION'
        ]['DATA']['LINES']['LINE']
        self.assertEquals(len(api_request_lines), 2)
        self.assertEquals(api_request_lines[0]['SKU'], self.product_1.sku)
        self.assertEquals(api_request_lines[1]['SKU'], self.product_2.sku)

    @responses.activate
    def test_add_or_update_outbound_failure(self):
        responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Outbound',
            json={
                'status': None,
                'MessageID': None,
                'Note': None,
                'errorCode': 'InvalidFormatData',
                'ErrorMessage': '',
            },
            status=200,  # orian responds with status 200 even with errors
        )

        result = add_or_update_outbound(
            Order.objects.get(pk=self.order_1.pk), datetime.now()
        )

        # result should be false since the mock api responded with an error
        self.assertEquals(result, False)

    @responses.activate
    def test_add_or_update_outbound_success(self):
        responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Outbound',
            json={
                'status': 'SUCCSESS',
                'MessageID': None,
                'Note': 'Outbound Created/Updated',
                'errorCode': None,
                'ErrorMessage': None,
            },
            status=200,  # orian responds with status 200 even with errors
        )

        result = add_or_update_outbound(
            Order.objects.get(pk=self.order_1.pk), datetime.now()
        )

        # result should be true since we succeeded
        self.assertEquals(result, True)

        api_request_body_json = json.loads(responses.calls[0].request.body)[
            'DATACOLLECTION'
        ]['DATA']

        # check that the reference order value sent to the api is correct
        api_reference_order = api_request_body_json['REFERENCEORD']
        self.assertEquals(api_reference_order, '')

        # check the the contact details sent to the api are correct
        api_request_contact = api_request_body_json['CONTACT']
        self.assertEquals(
            api_request_contact['STREET1'],
            format_street_line(
                self.order_1.delivery_street,
                self.order_1.delivery_street_number,
                self.order_1.delivery_apartment_number,
            ),
        )
        self.assertEquals(api_request_contact['CITY'], self.order_1.delivery_city)
        self.assertEquals(api_request_contact['CONTACT1NAME'], self.order_1.full_name)
        self.assertEquals(api_request_contact['CONTACT2NAME'], self.order_1.full_name)
        self.assertEquals(
            api_request_contact['CONTACT1PHONE'], self.order_1.phone_number
        )
        self.assertEquals(
            api_request_contact['CONTACT2PHONE'],
            self.order_1.additional_phone_number,
        )
        self.assertEquals(api_request_contact['CONTACT1EMAIL'], self.employee_1.email)
        self.assertEquals(api_request_contact['CONTACT2EMAIL'], '')

        # check that the address sent to the api is the order's address
        api_shipping_details = api_request_body_json['SHIPPINGDETAIL']
        self.assertEquals(
            api_shipping_details['DELIVERYCOMMENTS'],
            self.order_1.delivery_additional_details,
        )

        # check that the lines sent to the api contain the single product
        api_request_lines = api_request_body_json['LINES']['LINE']
        self.assertEquals(len(api_request_lines), 1)
        self.assertEquals(api_request_lines[0]['SKU'], self.product_1.sku)
        self.assertEquals(
            api_request_lines[0]['QTYORIGINAL'],
            self.order_1.orderproduct_set.all()[0].quantity,
        )

    @responses.activate
    def test_add_or_update_outbound_success_multiple_products(self):
        responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Outbound',
            json={
                'status': 'SUCCSESS',
                'MessageID': None,
                'Note': 'Outbound Created/Updated',
                'errorCode': None,
                'ErrorMessage': None,
            },
            status=200,  # orian responds with status 200 even with errors
        )

        result = add_or_update_outbound(
            Order.objects.get(pk=self.order_2.pk), datetime.now()
        )

        # result should be true since we succeeded
        self.assertEquals(result, True)

        api_request_body_json = json.loads(responses.calls[0].request.body)[
            'DATACOLLECTION'
        ]['DATA']

        # check that the reference order value sent to the api is correct
        api_reference_order = api_request_body_json['REFERENCEORD']
        self.assertEquals(api_reference_order, '')

        # check the the contact details sent to the api are correct
        api_request_contact = api_request_body_json['CONTACT']
        self.assertEquals(
            api_request_contact['STREET1'],
            format_street_line(
                self.order_2.delivery_street,
                self.order_2.delivery_street_number,
                self.order_2.delivery_apartment_number,
            ),
        )
        self.assertEquals(api_request_contact['CITY'], self.order_2.delivery_city)
        self.assertEquals(api_request_contact['CONTACT1NAME'], self.order_2.full_name)
        self.assertEquals(api_request_contact['CONTACT2NAME'], self.order_2.full_name)
        self.assertEquals(
            api_request_contact['CONTACT1PHONE'], self.order_2.phone_number
        )
        self.assertEquals(
            api_request_contact['CONTACT2PHONE'],
            self.order_2.additional_phone_number,
        )
        self.assertEquals(api_request_contact['CONTACT1EMAIL'], self.employee_1.email)
        self.assertEquals(api_request_contact['CONTACT2EMAIL'], '')

        # check that the address sent to the api is the order's address
        api_shipping_details = api_request_body_json['SHIPPINGDETAIL']
        self.assertEquals(
            api_shipping_details['DELIVERYCOMMENTS'],
            self.order_2.delivery_additional_details,
        )

        # check that the lines sent to the api contain the single product
        api_request_lines = api_request_body_json['LINES']['LINE']
        self.assertEquals(len(api_request_lines), 2)
        self.assertEquals(api_request_lines[0]['SKU'], self.product_1.sku)
        self.assertEquals(
            api_request_lines[0]['QTYORIGINAL'],
            self.order_2.orderproduct_set.all()[0].quantity,
        )
        self.assertEquals(api_request_lines[1]['SKU'], self.product_2.sku)
        self.assertEquals(
            api_request_lines[1]['QTYORIGINAL'],
            self.order_2.orderproduct_set.all()[1].quantity,
        )

    @responses.activate
    def test_add_or_update_outbound_success_office_delivery(self):
        responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Outbound',
            json={
                'status': 'SUCCSESS',
                'MessageID': None,
                'Note': 'Outbound Created/Updated',
                'errorCode': None,
                'ErrorMessage': None,
            },
            status=200,  # orian responds with status 200 even with errors
        )

        result = add_or_update_outbound(
            Order.objects.get(pk=self.order_3.pk), datetime.now()
        )

        # result should be true since we succeeded
        self.assertEquals(result, True)

        api_request_body_json = json.loads(responses.calls[0].request.body)[
            'DATACOLLECTION'
        ]['DATA']

        # check that the reference order value sent to the api is correct
        api_reference_order = api_request_body_json['REFERENCEORD']
        self.assertEquals(
            api_reference_order,
            _platform_id_to_orian_id(self.employee_group_campaign_2.pk),
        )

        # check the the contact details sent to the api are correct - since
        # this employee is in an office-delivery group the address should be
        # that of the office
        api_request_contact = api_request_body_json['CONTACT']
        self.assertEquals(
            api_request_contact['STREET1'],
            format_street_line(
                self.employee_group_2.delivery_street,
                self.employee_group_2.delivery_street_number,
                self.employee_group_2.delivery_apartment_number,
            ),
        )
        self.assertEquals(
            api_request_contact['CITY'], self.employee_group_2.delivery_city
        )
        self.assertEquals(
            api_request_contact['CONTACT1NAME'], self.organization.manager_full_name
        )
        self.assertEquals(
            api_request_contact['CONTACT2NAME'], self.employee_2.full_name
        )
        self.assertEquals(
            api_request_contact['CONTACT1PHONE'], self.organization.manager_phone_number
        )
        self.assertEquals(
            api_request_contact['CONTACT2PHONE'],
            self.employee_2.phone_number,
        )
        self.assertEquals(
            api_request_contact['CONTACT1EMAIL'], self.organization.manager_email
        )
        self.assertEquals(api_request_contact['CONTACT2EMAIL'], self.employee_2.email)

        # check that the address sent to the api is the order's address
        api_shipping_details = api_request_body_json['SHIPPINGDETAIL']
        self.assertEquals(
            api_shipping_details['DELIVERYCOMMENTS'],
            '',
        )

        # check that the lines sent to the api contain the single product
        api_request_lines = api_request_body_json['LINES']['LINE']
        self.assertEquals(len(api_request_lines), 1)
        self.assertEquals(api_request_lines[0]['SKU'], self.product_1.sku)
        self.assertEquals(
            api_request_lines[0]['QTYORIGINAL'],
            self.order_3.orderproduct_set.all()[0].quantity,
        )


@override_settings(
    ORIAN_BASE_URL='https://test.local',
    ORIAN_API_TOKEN='1234',
    ORIAN_CONSIGNEE='AAA',
)
class SendPurchaseOrderToLogisticsCenterTestCase(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            name='supplier name',
        )
        self.brand = Brand.objects.create(
            name='brand name',
        )
        self.product_1 = Product.objects.create(
            brand=self.brand,
            supplier=self.supplier,
            name='product 1 name',
            sku='1',
            cost_price=50,
            sale_price=60,
        )
        self.purchase_order_1 = PurchaseOrder.objects.create(supplier=self.supplier)
        PurchaseOrderProduct.objects.create(
            product_id=self.product_1,
            purchase_order=self.purchase_order_1,
            quantity_ordered=1,
            quantity_sent_to_logistics_center=0,
        )

    @patch(
        'logistics.signals.send_purchase_order_to_logistics_center', return_value=True
    )
    def test_task_triggered_on_purchase_order_approve(self, mock_send_task):
        # mock is not called to begin with
        self.assertEquals(mock_send_task.apply_async.call_count, 0)

        # we can take an existing purchase order and change its status to
        # anything but approved, or create a new purchase order with any status
        # besided approved and the task should not be invoked
        self.purchase_order_1.status = PurchaseOrder.Status.PENDING.name
        self.purchase_order_1.save()
        self.purchase_order_1.status = PurchaseOrder.Status.SENT_TO_SUPPLIER.name
        self.purchase_order_1.save()
        self.purchase_order_1.status = PurchaseOrder.Status.CANCELLED.name
        self.purchase_order_1.save()
        PurchaseOrder.objects.create(supplier=self.supplier)
        PurchaseOrder.objects.create(
            supplier=self.supplier,
            status=PurchaseOrder.Status.PENDING.name,
        )
        PurchaseOrder.objects.create(
            supplier=self.supplier,
            status=PurchaseOrder.Status.SENT_TO_SUPPLIER.name,
        )
        PurchaseOrder.objects.create(
            supplier=self.supplier,
            status=PurchaseOrder.Status.CANCELLED.name,
        )

        # mock was not yet called
        self.assertEquals(mock_send_task.apply_async.call_count, 0)

        # update an existing purchase order's status to approved and the task
        # should be invoked
        self.purchase_order_1.status = PurchaseOrder.Status.APPROVED.name
        self.purchase_order_1.save()

        # mock was called
        self.assertEquals(mock_send_task.apply_async.call_count, 1)
        self.assertEquals(
            mock_send_task.apply_async.call_args[0], ((self.purchase_order_1.pk,),)
        )

        # create a new purchase order with an approved status and the task
        # should be invoked again
        new_purchase_order = PurchaseOrder.objects.create(
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED.name,
        )

        # mock was called again
        self.assertEquals(mock_send_task.apply_async.call_count, 2)
        self.assertEquals(
            mock_send_task.apply_async.call_args[0], ((new_purchase_order.pk,),)
        )

    @responses.activate
    @override_settings(ORIAN_MESSAGE_TIMEZONE_NAME='UTC')
    def test_task_api_calls(self):
        supplier_api_mock = responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Company',
            json={
                'status': 'SUCCSESS',
                'MessageID': None,
                'Note': 'Company Created/Updated',
                'errorCode': None,
                'ErrorMessage': None,
            },
            status=200,  # orian responds with status 200 even with errors
        )
        product_api_mock = responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Sku',
            json={
                'status': 'SUCCSESS',
                'MessageID': None,
                'Note': 'Sku Importer sent',
                'errorCode': None,
                'ErrorMessage': None,
            },
            status=200,  # orian responds with status 200 even with errors
        )
        inbound_api_mock = responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Inbound',
            json={
                'status': 'SUCCSESS',
                'MessageID': None,
                'Note': 'Inbound Created/Updated',
                'errorCode': None,
                'ErrorMessage': None,
            },
            status=200,  # orian responds with status 200 even with errors
        )

        # the sent_to_logistics_center_at field should be None
        self.purchase_order_1.refresh_from_db()
        self.assertEquals(self.purchase_order_1.sent_to_logistics_center_at, None)

        self.purchase_order_1.status = PurchaseOrder.Status.APPROVED.name
        self.purchase_order_1.save()

        # each mock should have been called once
        self.assertEquals(len(supplier_api_mock.calls), 1)
        self.assertEquals(len(product_api_mock.calls), 1)
        self.assertEquals(len(inbound_api_mock.calls), 1)

        # the sent_to_logistics_center_at field was set and is within the last
        # 5 seconds
        self.purchase_order_1.refresh_from_db()
        self.assertGreaterEqual(
            self.purchase_order_1.sent_to_logistics_center_at,
            datetime.now(timezone.utc) - timedelta(seconds=5),
        )


@override_settings(
    ORIAN_BASE_URL='https://test.local',
    ORIAN_API_TOKEN='1234',
    ORIAN_CONSIGNEE='AAA',
)
class SendOrderToLogisticsCenterTestCase(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            name='supplier name',
        )
        self.brand = Brand.objects.create(
            name='brand name',
        )
        self.product_1 = Product.objects.create(
            brand=self.brand,
            supplier=self.supplier,
            name='product 1 name',
            sku='1',
            cost_price=50,
            sale_price=60,
        )

        # create the campaign infrastructure for the orders we need
        organization = Organization.objects.create(
            name='Test organization',
            manager_full_name='Test manager',
            manager_phone_number='0500000009',
            manager_email='manager@test.test',
        )
        campaign = Campaign.objects.create(
            name='Test campaign',
            organization=organization,
            status=Campaign.CampaignStatusEnum.ACTIVE.name,
            start_date_time=datetime.now(),
            end_date_time=datetime.now(),
        )
        employee_group = EmployeeGroup.objects.create(
            name='Test employee group 1',
            organization=organization,
            delivery_city='Office1',
            delivery_street='Office street 1',
            delivery_street_number='1',
            delivery_apartment_number='2',
            delivery_location=DeliveryLocationEnum.ToHome.name,
        )
        employee_group_campaign = EmployeeGroupCampaign.objects.create(
            employee_group=employee_group, campaign=campaign, budget_per_employee=100
        )
        employee = Employee.objects.create(
            employee_group=employee_group,
            first_name='Test',
            last_name='Employee 1',
            email='test1@test.test',
        )
        employee_group_campaign_product = EmployeeGroupCampaignProduct.objects.create(
            employee_group_campaign_id=employee_group_campaign,
            product_id=self.product_1,
        )

        # the order we need for testing outbound
        self.order = Order.objects.create(
            campaign_employee_id=CampaignEmployee.objects.get(
                campaign=campaign, employee=employee
            ),
            order_date_time=datetime.now(),
            cost_from_budget=100,
            cost_added=0,
            status=Order.OrderStatusEnum.PENDING.name,
            full_name='Test name 1',
            phone_number='0500000000',
            additional_phone_number='050000001',
            delivery_city='City1',
            delivery_street='Main1',
            delivery_street_number='1',
            delivery_apartment_number='1',
            delivery_additional_details='Additional 1',
        )
        OrderProduct.objects.create(
            order_id=self.order,
            product_id=employee_group_campaign_product,
            quantity=1,
        )

    @responses.activate
    def test_task_add_or_update_dummy_company_failure(self):
        responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Company',
            json={
                'status': None,
                'MessageID': None,
                'Note': None,
                'errorCode': 'InvalidFormatData',
                'ErrorMessage': '',
            },
            status=200,  # orian responds with status 200 even with errors
        )

        with self.assertRaises(Exception):
            send_order_to_logistics_center.apply_async((self.order.pk,))

    @responses.activate
    def test_task_add_or_update_outbound_failure(self):
        responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Company',
            json={
                'status': 'SUCCSESS',
                'MessageID': None,
                'Note': 'Company Created/Updated',
                'errorCode': None,
                'ErrorMessage': None,
            },
            status=200,  # orian responds with status 200 even with errors
        )
        responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Outbound',
            json={
                'status': None,
                'MessageID': None,
                'Note': None,
                'errorCode': 'InvalidFormatData',
                'ErrorMessage': '',
            },
            status=200,  # orian responds with status 200 even with errors
        )

        with self.assertRaises(Exception):
            send_order_to_logistics_center.apply_async((self.order.pk,))

    @responses.activate
    @override_settings(ORIAN_MESSAGE_TIMEZONE_NAME='UTC')
    def test_task_api_calls(self):
        dummy_company_api_mock = responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Company',
            json={
                'status': 'SUCCSESS',
                'MessageID': None,
                'Note': 'Company Created/Updated',
                'errorCode': None,
                'ErrorMessage': None,
            },
            status=200,  # orian responds with status 200 even with errors
        )
        outbound_api_mock = responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Outbound',
            json={
                'status': 'SUCCSESS',
                'MessageID': None,
                'Note': 'Outbound Created/Updated',
                'errorCode': None,
                'ErrorMessage': None,
            },
            status=200,  # orian responds with status 200 even with errors
        )

        # the status field should be pending
        self.order.refresh_from_db()
        self.assertEquals(self.order.status, Order.OrderStatusEnum.PENDING.name)

        send_order_to_logistics_center.apply_async((self.order.pk,))

        # each mock should have been called once
        self.assertEquals(len(dummy_company_api_mock.calls), 1)
        self.assertEquals(len(outbound_api_mock.calls), 1)

        # the status field was set to sent to logistics center
        self.order.refresh_from_db()
        self.assertEquals(
            self.order.status,
            Order.OrderStatusEnum.SENT_TO_LOGISTIC_CENTER.name,
        )


@override_settings(
    ORIAN_BASE_URL='https://test.local',
    ORIAN_API_TOKEN='1234',
    ORIAN_CONSIGNEE='AAA',
)
class SyncProductWithLogisticsCenterTestCase(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            name='supplier name',
        )
        self.brand = Brand.objects.create(
            name='brand name',
        )
        self.product_1 = Product.objects.create(
            brand=self.brand,
            supplier=self.supplier,
            name='product 1 name',
            sku='1',
            cost_price=50,
            sale_price=60,
        )

    @responses.activate
    def test_task_add_or_update_product_failure(self):
        responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Sku',
            json={
                'status': None,
                'MessageID': None,
                'Note': None,
                'errorCode': 'InvalidFormatData',
                'ErrorMessage': '',
            },
            status=200,  # orian responds with status 200 even with errors
        )

        with self.assertRaises(Exception):
            sync_product_with_logistics_center.apply_async((self.order.pk,))

    @responses.activate
    @override_settings(ORIAN_MESSAGE_TIMEZONE_NAME='UTC')
    def test_task_api_calls(self):
        product_api_mock = responses.add(
            responses.POST,
            f'{settings.ORIAN_BASE_URL}/Sku',
            json={
                'status': 'SUCCSESS',
                'MessageID': None,
                'Note': 'Sku Importer sent',
                'errorCode': None,
                'ErrorMessage': None,
            },
            status=200,  # orian responds with status 200 even with errors
        )

        sync_product_with_logistics_center.apply_async((self.product_1.pk,))

        # each mock should have been called once
        self.assertEquals(len(product_api_mock.calls), 1)


class ProcessLogisticsCenterInboundReceiptMessageTestCase(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            name='supplier name',
        )
        self.brand = Brand.objects.create(
            name='brand name',
        )
        self.product_1 = Product.objects.create(
            brand=self.brand,
            supplier=self.supplier,
            name='product 1 name',
            sku='1',
            cost_price=50,
            sale_price=60,
        )
        self.product_2 = Product.objects.create(
            brand=self.brand,
            supplier=self.supplier,
            name='product 2 name',
            sku='2',
            cost_price=70,
            sale_price=80,
        )
        self.purchase_order_1 = PurchaseOrder.objects.create(supplier=self.supplier)
        self.purchase_order_product_1 = PurchaseOrderProduct.objects.create(
            product_id=self.product_1,
            purchase_order=self.purchase_order_1,
            quantity_ordered=1,
            quantity_sent_to_logistics_center=0,
        )
        self.purchase_order_2 = PurchaseOrder.objects.create(supplier=self.supplier)
        self.purchase_order_product_2 = PurchaseOrderProduct.objects.create(
            product_id=self.product_1,
            purchase_order=self.purchase_order_2,
            quantity_ordered=2,
            quantity_sent_to_logistics_center=0,
        )
        self.purchase_order_product_3 = PurchaseOrderProduct.objects.create(
            product_id=self.product_2,
            purchase_order=self.purchase_order_2,
            quantity_ordered=3,
            quantity_sent_to_logistics_center=0,
        )
        self.logistics_center_message_invalid_1 = LogisticsCenterMessage.objects.create(
            center=LogisticsCenterEnum.ORIAN.name,
            message_type=LogisticsCenterMessageTypeEnum.INBOUND_RECEIPT.name,
            raw_body='{}',
        )
        self.logistics_center_message_invalid_2 = LogisticsCenterMessage.objects.create(
            center=LogisticsCenterEnum.ORIAN.name,
            message_type=LogisticsCenterMessageTypeEnum.INBOUND_RECEIPT.name,
            raw_body=json.dumps({'DATACOLLECTION': {'DATA': {}}}),
        )
        self.logistics_center_message_no_lines = LogisticsCenterMessage.objects.create(
            center=LogisticsCenterEnum.ORIAN.name,
            message_type=LogisticsCenterMessageTypeEnum.INBOUND_RECEIPT.name,
            raw_body=json.dumps(
                {
                    'DATACOLLECTION': {
                        'DATA': {
                            'RECEIPT': 'CODE1',
                            'STARTRECEIPTDATE': '8/1/2024 12:00:00 PM',
                            'CLOSERECEIPTDATE': '8/1/2024 12:00:00 PM',
                            'LINES': {'LINE': []},
                        }
                    }
                }
            ),
        )
        self.logistics_center_message_non_existing_order = (
            LogisticsCenterMessage.objects.create(
                center=LogisticsCenterEnum.ORIAN.name,
                message_type=LogisticsCenterMessageTypeEnum.INBOUND_RECEIPT.name,
                raw_body=json.dumps(
                    {
                        'DATACOLLECTION': {
                            'DATA': {
                                'RECEIPT': 'CODE2',
                                'STARTRECEIPTDATE': '8/1/2024 12:00:00 PM',
                                'CLOSERECEIPTDATE': '8/1/2024 12:00:00 PM',
                                'LINES': {
                                    'LINE': [
                                        {
                                            'RECEIPTLINE': '1',
                                            'CONSIGNEE': 'NKS',
                                            'SKU': '0',
                                            'ORDERID': _platform_id_to_orian_id(0),
                                            'ORDERLINE': '0',
                                            'QTYEXPECTED': '1.0000',
                                            'QTYRECEIVED': '1.0000',
                                            'QTYORIGINAL': '1.0000',
                                            'DOCUMENTTYPE': 'INBOUND',
                                            'UNITPRICE': '0',
                                            'INPUTQTY': '0.0000',
                                            'INPUTSKU': '',
                                            'INPUTUOM': '',
                                            'REFERENCEORDER': '',
                                            'REFERENCEORDERLINE': '0',
                                            'INVENTORYSTATUS': 'AVAILABLE',
                                            'COMPANY': _platform_id_to_orian_id(0),
                                            'COMPANYTYPE': 'VENDOR',
                                            'ASNS': None,
                                            'LOADS': {
                                                'LOAD': {
                                                    'LOADID': '111111111111',
                                                    'UOM': 'EACH',
                                                    'QTY': '1.0000',
                                                    'STATUS': 'AVAILABLE',
                                                    'LOADATTRIBUTES': None,
                                                }
                                            },
                                        },
                                    ]
                                },
                            }
                        }
                    }
                ),
            )
        )
        self.logistics_center_message_single_line = (
            LogisticsCenterMessage.objects.create(
                center=LogisticsCenterEnum.ORIAN.name,
                message_type=LogisticsCenterMessageTypeEnum.INBOUND_RECEIPT.name,
                raw_body=json.dumps(
                    {
                        'DATACOLLECTION': {
                            'DATA': {
                                'RECEIPT': 'CODE3',
                                'STARTRECEIPTDATE': '8/1/2024 12:00:00 PM',
                                'CLOSERECEIPTDATE': '8/1/2024 12:00:00 PM',
                                'LINES': {
                                    'LINE': {
                                        'RECEIPTLINE': '1',
                                        'CONSIGNEE': 'NKS',
                                        'SKU': self.product_1.sku,
                                        'ORDERID': _platform_id_to_orian_id(
                                            self.purchase_order_1.pk
                                        ),
                                        'ORDERLINE': '0',
                                        'QTYEXPECTED': '1.0000',
                                        'QTYRECEIVED': '1.0000',
                                        'QTYORIGINAL': '1.0000',
                                        'DOCUMENTTYPE': 'INBOUND',
                                        'UNITPRICE': '0',
                                        'INPUTQTY': '0.0000',
                                        'INPUTSKU': '',
                                        'INPUTUOM': '',
                                        'REFERENCEORDER': '',
                                        'REFERENCEORDERLINE': '0',
                                        'INVENTORYSTATUS': 'AVAILABLE',
                                        'COMPANY': _platform_id_to_orian_id(
                                            self.supplier.pk
                                        ),
                                        'COMPANYTYPE': 'VENDOR',
                                        'ASNS': None,
                                        'LOADS': {
                                            'LOAD': {
                                                'LOADID': '111111111111',
                                                'UOM': 'EACH',
                                                'QTY': '1.0000',
                                                'STATUS': 'AVAILABLE',
                                                'LOADATTRIBUTES': None,
                                            }
                                        },
                                    },
                                },
                            }
                        }
                    }
                ),
            )
        )
        self.logistics_center_message_multi_line = (
            LogisticsCenterMessage.objects.create(
                center=LogisticsCenterEnum.ORIAN.name,
                message_type=LogisticsCenterMessageTypeEnum.INBOUND_RECEIPT.name,
                raw_body=json.dumps(
                    {
                        'DATACOLLECTION': {
                            'DATA': {
                                'RECEIPT': 'CODE4',
                                'STARTRECEIPTDATE': '8/1/2024 12:00:00 PM',
                                'CLOSERECEIPTDATE': '8/1/2024 12:00:00 PM',
                                'LINES': {
                                    'LINE': [
                                        {
                                            'RECEIPTLINE': '1',
                                            'CONSIGNEE': 'NKS',
                                            'SKU': self.product_1.sku,
                                            'ORDERID': _platform_id_to_orian_id(
                                                self.purchase_order_2.pk
                                            ),
                                            'ORDERLINE': '0',
                                            'QTYEXPECTED': '3.0000',
                                            'QTYRECEIVED': '3.0000',
                                            'QTYORIGINAL': '3.0000',
                                            'DOCUMENTTYPE': 'INBOUND',
                                            'UNITPRICE': '0',
                                            'INPUTQTY': '0.0000',
                                            'INPUTSKU': '',
                                            'INPUTUOM': '',
                                            'REFERENCEORDER': '',
                                            'REFERENCEORDERLINE': '0',
                                            'INVENTORYSTATUS': 'AVAILABLE',
                                            'COMPANY': _platform_id_to_orian_id(
                                                self.supplier.pk
                                            ),
                                            'COMPANYTYPE': 'VENDOR',
                                            'ASNS': None,
                                            'LOADS': {
                                                'LOAD': {
                                                    'LOADID': '111111111111',
                                                    'UOM': 'EACH',
                                                    'QTY': '3.0000',
                                                    'STATUS': 'AVAILABLE',
                                                    'LOADATTRIBUTES': None,
                                                }
                                            },
                                        },
                                        {
                                            'RECEIPTLINE': '2',
                                            'CONSIGNEE': 'NKS',
                                            'SKU': self.product_2.sku,
                                            'ORDERID': _platform_id_to_orian_id(
                                                self.purchase_order_2.pk
                                            ),
                                            'ORDERLINE': '1',
                                            'QTYEXPECTED': '15.0000',
                                            'QTYRECEIVED': '15.0000',
                                            'QTYORIGINAL': '15.0000',
                                            'DOCUMENTTYPE': 'INBOUND',
                                            'UNITPRICE': '0',
                                            'INPUTQTY': '0.0000',
                                            'INPUTSKU': '',
                                            'INPUTUOM': '',
                                            'REFERENCEORDER': '',
                                            'REFERENCEORDERLINE': '0',
                                            'INVENTORYSTATUS': 'AVAILABLE',
                                            'COMPANY': _platform_id_to_orian_id(
                                                self.supplier.pk
                                            ),
                                            'COMPANYTYPE': 'VENDOR',
                                            'ASNS': None,
                                            'LOADS': {
                                                'LOAD': {
                                                    'LOADID': '111111111111',
                                                    'UOM': 'EACH',
                                                    'QTY': '15.0000',
                                                    'STATUS': 'AVAILABLE',
                                                    'LOADATTRIBUTES': None,
                                                }
                                            },
                                        },
                                    ]
                                },
                            }
                        }
                    }
                ),
            )
        )
        self.logistics_center_message_multi_line_quantity_update = (
            LogisticsCenterMessage.objects.create(
                center=LogisticsCenterEnum.ORIAN.name,
                message_type=LogisticsCenterMessageTypeEnum.INBOUND_RECEIPT.name,
                raw_body=json.dumps(
                    {
                        'DATACOLLECTION': {
                            'DATA': {
                                'RECEIPT': 'CODE4',
                                'STARTRECEIPTDATE': '8/2/2024 4:00:00 PM',
                                'CLOSERECEIPTDATE': '8/2/2024 4:00:00 PM',
                                'LINES': {
                                    'LINE': [
                                        {
                                            'RECEIPTLINE': '1',
                                            'CONSIGNEE': 'NKS',
                                            'SKU': self.product_1.sku,
                                            'ORDERID': _platform_id_to_orian_id(
                                                self.purchase_order_2.pk
                                            ),
                                            'ORDERLINE': '0',
                                            'QTYEXPECTED': '30.0000',
                                            'QTYRECEIVED': '30.0000',
                                            'QTYORIGINAL': '30.0000',
                                            'DOCUMENTTYPE': 'INBOUND',
                                            'UNITPRICE': '0',
                                            'INPUTQTY': '0.0000',
                                            'INPUTSKU': '',
                                            'INPUTUOM': '',
                                            'REFERENCEORDER': '',
                                            'REFERENCEORDERLINE': '0',
                                            'INVENTORYSTATUS': 'AVAILABLE',
                                            'COMPANY': _platform_id_to_orian_id(
                                                self.supplier.pk
                                            ),
                                            'COMPANYTYPE': 'VENDOR',
                                            'ASNS': None,
                                            'LOADS': {
                                                'LOAD': {
                                                    'LOADID': '111111111111',
                                                    'UOM': 'EACH',
                                                    'QTY': '30.0000',
                                                    'STATUS': 'AVAILABLE',
                                                    'LOADATTRIBUTES': None,
                                                }
                                            },
                                        },
                                        {
                                            'RECEIPTLINE': '2',
                                            'CONSIGNEE': 'NKS',
                                            'SKU': self.product_2.sku,
                                            'ORDERID': _platform_id_to_orian_id(
                                                self.purchase_order_2.pk
                                            ),
                                            'ORDERLINE': '1',
                                            'QTYEXPECTED': '150.0000',
                                            'QTYRECEIVED': '150.0000',
                                            'QTYORIGINAL': '150.0000',
                                            'DOCUMENTTYPE': 'INBOUND',
                                            'UNITPRICE': '0',
                                            'INPUTQTY': '0.0000',
                                            'INPUTSKU': '',
                                            'INPUTUOM': '',
                                            'REFERENCEORDER': '',
                                            'REFERENCEORDERLINE': '0',
                                            'INVENTORYSTATUS': 'AVAILABLE',
                                            'COMPANY': _platform_id_to_orian_id(
                                                self.supplier.pk
                                            ),
                                            'COMPANYTYPE': 'VENDOR',
                                            'ASNS': None,
                                            'LOADS': {
                                                'LOAD': {
                                                    'LOADID': '111111111111',
                                                    'UOM': 'EACH',
                                                    'QTY': '150.0000',
                                                    'STATUS': 'AVAILABLE',
                                                    'LOADATTRIBUTES': None,
                                                }
                                            },
                                        },
                                    ]
                                },
                            }
                        }
                    }
                ),
            )
        )

    def test_message_not_found(self):
        # the task should raise errors if any occur that cannot be handled so
        # we know it is failing
        with self.assertRaises(LogisticsCenterMessage.DoesNotExist):
            process_logistics_center_message.apply_async((123,))

    def test_invalid_message(self):
        # the task should raise errors if any occur that cannot be handled so
        # we know it is failing
        with self.assertRaises(KeyError):
            process_logistics_center_message.apply_async(
                (self.logistics_center_message_invalid_1.pk,)
            )

        with self.assertRaises(KeyError):
            process_logistics_center_message.apply_async(
                (self.logistics_center_message_invalid_2.pk,)
            )

    def test_no_receipt_lines(self):
        process_logistics_center_message.apply_async(
            (self.logistics_center_message_no_lines.pk,)
        )

        # a receipt was created
        self.assertEquals(len(LogisticsCenterInboundReceipt.objects.all()), 1)
        self.assertEquals(
            LogisticsCenterInboundReceipt.objects.first().receipt_code, 'CODE1'
        )

        # no lines were created since none were received
        self.assertEquals(len(LogisticsCenterInboundReceiptLine.objects.all()), 0)

    def test_order_id_not_found(self):
        with self.assertRaises(PurchaseOrderProduct.DoesNotExist):
            process_logistics_center_message.apply_async(
                (self.logistics_center_message_non_existing_order.pk,)
            )

        # a receipt was still created
        self.assertEquals(len(LogisticsCenterInboundReceipt.objects.all()), 1)
        self.assertEquals(
            LogisticsCenterInboundReceipt.objects.first().receipt_code, 'CODE2'
        )

        # no receipt lines should have been created due to the error
        self.assertEquals(len(LogisticsCenterInboundReceiptLine.objects.all()), 0)

    def test_single_line_receipt(self):
        process_logistics_center_message.apply_async(
            (self.logistics_center_message_single_line.pk,)
        )

        # a receipt was created
        self.assertEquals(len(LogisticsCenterInboundReceipt.objects.all()), 1)
        receipt = LogisticsCenterInboundReceipt.objects.first()
        self.assertEquals(receipt.receipt_code, 'CODE3')

        # a receipt line was also created
        self.assertEquals(len(LogisticsCenterInboundReceiptLine.objects.all()), 1)
        receipt_line = LogisticsCenterInboundReceiptLine.objects.first()
        self.assertEquals(receipt_line.receipt, receipt)
        self.assertEquals(receipt_line.receipt_line, 1)
        self.assertEquals(
            receipt_line.purchase_order_product, self.purchase_order_product_1
        )
        self.assertEquals(receipt_line.quantity_received, 1)
        self.assertEquals(
            receipt_line.logistics_center_message,
            self.logistics_center_message_single_line,
        )

    def test_multi_line_receipt(self):
        process_logistics_center_message.apply_async(
            (self.logistics_center_message_multi_line.pk,)
        )

        # a receipt was created
        self.assertEquals(len(LogisticsCenterInboundReceipt.objects.all()), 1)
        receipt = LogisticsCenterInboundReceipt.objects.first()
        self.assertEquals(receipt.receipt_code, 'CODE4')

        # two receipt lines were also created
        self.assertEquals(len(LogisticsCenterInboundReceiptLine.objects.all()), 2)
        receipt_line_1 = LogisticsCenterInboundReceiptLine.objects.order_by(
            'receipt_line'
        ).all()[0]
        self.assertEquals(receipt_line_1.receipt, receipt)
        self.assertEquals(receipt_line_1.receipt_line, 1)
        self.assertEquals(
            receipt_line_1.purchase_order_product, self.purchase_order_product_2
        )
        self.assertEquals(receipt_line_1.quantity_received, 3)
        self.assertEquals(
            receipt_line_1.logistics_center_message,
            self.logistics_center_message_multi_line,
        )
        receipt_line_2 = LogisticsCenterInboundReceiptLine.objects.order_by(
            'receipt_line'
        ).all()[1]
        self.assertEquals(receipt_line_2.receipt, receipt)
        self.assertEquals(receipt_line_2.receipt_line, 2)
        self.assertEquals(
            receipt_line_2.purchase_order_product, self.purchase_order_product_3
        )
        self.assertEquals(receipt_line_2.quantity_received, 15)
        self.assertEquals(
            receipt_line_2.logistics_center_message,
            self.logistics_center_message_multi_line,
        )

    def test_multi_line_receipt_with_update(self):
        process_logistics_center_message.apply_async(
            (self.logistics_center_message_multi_line.pk,)
        )

        # a receipt was created
        self.assertEquals(len(LogisticsCenterInboundReceipt.objects.all()), 1)
        receipt = LogisticsCenterInboundReceipt.objects.first()
        self.assertEquals(receipt.receipt_code, 'CODE4')
        self.assertEquals(
            receipt.receipt_start_date,
            datetime(year=2024, month=8, day=1, hour=12, minute=0, tzinfo=timezone.utc),
        )
        self.assertEquals(
            receipt.receipt_close_date,
            datetime(year=2024, month=8, day=1, hour=12, minute=0, tzinfo=timezone.utc),
        )

        # two receipt lines were also created
        self.assertEquals(len(LogisticsCenterInboundReceiptLine.objects.all()), 2)
        receipt_line_1 = LogisticsCenterInboundReceiptLine.objects.order_by(
            'receipt_line'
        ).all()[0]
        self.assertEquals(receipt_line_1.receipt, receipt)
        self.assertEquals(receipt_line_1.receipt_line, 1)
        self.assertEquals(
            receipt_line_1.purchase_order_product, self.purchase_order_product_2
        )
        self.assertEquals(receipt_line_1.quantity_received, 3)
        self.assertEquals(
            receipt_line_1.logistics_center_message,
            self.logistics_center_message_multi_line,
        )
        receipt_line_2 = LogisticsCenterInboundReceiptLine.objects.order_by(
            'receipt_line'
        ).all()[1]
        self.assertEquals(receipt_line_2.receipt, receipt)
        self.assertEquals(receipt_line_2.receipt_line, 2)
        self.assertEquals(
            receipt_line_2.purchase_order_product, self.purchase_order_product_3
        )
        self.assertEquals(receipt_line_2.quantity_received, 15)
        self.assertEquals(
            receipt_line_2.logistics_center_message,
            self.logistics_center_message_multi_line,
        )

        # process message for the same receipt with quantity updates (this
        # should not occur in real life, but can be handled nontheless)
        process_logistics_center_message.apply_async(
            (self.logistics_center_message_multi_line_quantity_update.pk,)
        )

        # another receipt was not created
        self.assertEquals(len(LogisticsCenterInboundReceipt.objects.all()), 1)
        receipt = LogisticsCenterInboundReceipt.objects.first()
        self.assertEquals(receipt.receipt_code, 'CODE4')
        self.assertEquals(
            receipt.receipt_start_date,
            datetime(year=2024, month=8, day=2, hour=16, minute=0, tzinfo=timezone.utc),
        )
        self.assertEquals(
            receipt.receipt_close_date,
            datetime(year=2024, month=8, day=2, hour=16, minute=0, tzinfo=timezone.utc),
        )

        # the two receipt lines were updated
        self.assertEquals(len(LogisticsCenterInboundReceiptLine.objects.all()), 2)
        receipt_line_1 = LogisticsCenterInboundReceiptLine.objects.order_by(
            'receipt_line'
        ).all()[0]
        self.assertEquals(receipt_line_1.receipt, receipt)
        self.assertEquals(receipt_line_1.receipt_line, 1)
        self.assertEquals(
            receipt_line_1.purchase_order_product, self.purchase_order_product_2
        )
        self.assertEquals(receipt_line_1.quantity_received, 30)
        self.assertEquals(
            receipt_line_1.logistics_center_message,
            self.logistics_center_message_multi_line_quantity_update,
        )
        receipt_line_2 = LogisticsCenterInboundReceiptLine.objects.order_by(
            'receipt_line'
        ).all()[1]
        self.assertEquals(receipt_line_2.receipt, receipt)
        self.assertEquals(receipt_line_2.receipt_line, 2)
        self.assertEquals(
            receipt_line_2.purchase_order_product, self.purchase_order_product_3
        )
        self.assertEquals(receipt_line_2.quantity_received, 150)
        self.assertEquals(
            receipt_line_2.logistics_center_message,
            self.logistics_center_message_multi_line_quantity_update,
        )


class ProcessLogisticsCenterOrderStatusChangeMessageTestCase(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            name='supplier name',
        )
        self.brand = Brand.objects.create(
            name='brand name',
        )
        self.product_1 = Product.objects.create(
            brand=self.brand,
            supplier=self.supplier,
            name='product 1 name',
            sku='1',
            cost_price=50,
            sale_price=60,
        )
        self.product_2 = Product.objects.create(
            brand=self.brand,
            supplier=self.supplier,
            name='product 2 name',
            sku='2',
            cost_price=70,
            sale_price=80,
        )

        # create the campaign infrastructure for the orders we need
        organization = Organization.objects.create(
            name='Test organization',
            manager_full_name='Test manager',
            manager_phone_number='0500000009',
            manager_email='manager@test.test',
        )
        campaign = Campaign.objects.create(
            name='Test campaign',
            organization=organization,
            status=Campaign.CampaignStatusEnum.ACTIVE.name,
            start_date_time=datetime.now(),
            end_date_time=datetime.now(),
        )
        employee_group = EmployeeGroup.objects.create(
            name='Test employee group 1',
            organization=organization,
            delivery_city='Office1',
            delivery_street='Office street 1',
            delivery_street_number='1',
            delivery_apartment_number='2',
            delivery_location=DeliveryLocationEnum.ToHome.name,
        )
        employee_group_campaign = EmployeeGroupCampaign.objects.create(
            employee_group=employee_group, campaign=campaign, budget_per_employee=100
        )
        employee = Employee.objects.create(
            employee_group=employee_group,
            first_name='Test',
            last_name='Employee 1',
            email='test1@test.test',
        )
        employee_group_campaign_product = EmployeeGroupCampaignProduct.objects.create(
            employee_group_campaign_id=employee_group_campaign,
            product_id=self.product_1,
        )

        # the order we need for testing outbound
        self.order = Order.objects.create(
            campaign_employee_id=CampaignEmployee.objects.get(
                campaign=campaign, employee=employee
            ),
            order_date_time=datetime.now(),
            cost_from_budget=100,
            cost_added=0,
            status=Order.OrderStatusEnum.PENDING.name,
            full_name='Test name 1',
            phone_number='0500000000',
            additional_phone_number='050000001',
            delivery_city='City1',
            delivery_street='Main1',
            delivery_street_number='1',
            delivery_apartment_number='1',
            delivery_additional_details='Additional 1',
        )
        OrderProduct.objects.create(
            order_id=self.order,
            product_id=employee_group_campaign_product,
            quantity=1,
        )

        self.logistics_center_message_invalid_1 = LogisticsCenterMessage.objects.create(
            center=LogisticsCenterEnum.ORIAN.name,
            message_type=LogisticsCenterMessageTypeEnum.ORDER_STATUS_CHANGE.name,
            raw_body='{}',
        )
        self.logistics_center_message_invalid_2 = LogisticsCenterMessage.objects.create(
            center=LogisticsCenterEnum.ORIAN.name,
            message_type=LogisticsCenterMessageTypeEnum.ORDER_STATUS_CHANGE.name,
            raw_body=json.dumps({'DATACOLLECTION': {'DATA': {}}}),
        )
        self.logistics_center_message_non_existing_order = (
            LogisticsCenterMessage.objects.create(
                center=LogisticsCenterEnum.ORIAN.name,
                message_type=LogisticsCenterMessageTypeEnum.ORDER_STATUS_CHANGE.name,
                raw_body=json.dumps(
                    {
                        'DATACOLLECTION': {
                            'DATA': {
                                'CONSIGNEE': 'NKS',
                                'ORDERID': 'unknown',
                                'ORDERTYPE': 'CUSTOMER',
                                'TOSTATUS': 'RECEIVED',
                                'STATUSDATE': '8/1/2024 12:00:00 PM',
                            }
                        }
                    }
                ),
            )
        )
        self.logistics_center_message_order_picked_status_update = (
            LogisticsCenterMessage.objects.create(
                center=LogisticsCenterEnum.ORIAN.name,
                message_type=LogisticsCenterMessageTypeEnum.ORDER_STATUS_CHANGE.name,
                raw_body=json.dumps(
                    {
                        'DATACOLLECTION': {
                            'DATA': {
                                'CONSIGNEE': 'NKS',
                                'ORDERID': Order.objects.get(pk=self.order.pk).order_id,
                                'ORDERTYPE': 'CUSTOMER',
                                'TOSTATUS': 'PICKED',
                                'STATUSDATE': '8/1/2024 12:00:00 PM',
                            }
                        }
                    }
                ),
            )
        )
        self.logistics_center_message_order_transported_status_update = (
            LogisticsCenterMessage.objects.create(
                center=LogisticsCenterEnum.ORIAN.name,
                message_type=LogisticsCenterMessageTypeEnum.ORDER_STATUS_CHANGE.name,
                raw_body=json.dumps(
                    {
                        'DATACOLLECTION': {
                            'DATA': {
                                'CONSIGNEE': 'NKS',
                                'ORDERID': Order.objects.get(pk=self.order.pk).order_id,
                                'ORDERTYPE': 'CUSTOMER',
                                'TOSTATUS': 'TRANSPORTED',
                                'STATUSDATE': '8/2/2024 12:00:00 PM',
                            }
                        }
                    }
                ),
            )
        )
        self.logistics_center_message_order_received_late_status_update = (
            LogisticsCenterMessage.objects.create(
                center=LogisticsCenterEnum.ORIAN.name,
                message_type=LogisticsCenterMessageTypeEnum.ORDER_STATUS_CHANGE.name,
                raw_body=json.dumps(
                    {
                        'DATACOLLECTION': {
                            'DATA': {
                                'CONSIGNEE': 'NKS',
                                'ORDERID': Order.objects.get(pk=self.order.pk).order_id,
                                'ORDERTYPE': 'CUSTOMER',
                                'TOSTATUS': 'RECEIVED',
                                'STATUSDATE': '8/1/2024 06:00:00 AM',
                            }
                        }
                    }
                ),
            )
        )
        self.logistics_center_message_order_picked_status_update_deprecated_id = (
            LogisticsCenterMessage.objects.create(
                center=LogisticsCenterEnum.ORIAN.name,
                message_type=LogisticsCenterMessageTypeEnum.ORDER_STATUS_CHANGE.name,
                raw_body=json.dumps(
                    {
                        'DATACOLLECTION': {
                            'DATA': {
                                'CONSIGNEE': 'NKS',
                                'ORDERID': _platform_id_to_orian_id(self.order.pk),
                                'ORDERTYPE': 'CUSTOMER',
                                'TOSTATUS': 'PICKED',
                                'STATUSDATE': '8/1/2024 12:00:00 PM',
                            }
                        }
                    }
                ),
            )
        )

    def test_message_not_found(self):
        # the task should raise errors if any occur that cannot be handled so
        # we know it is failing
        with self.assertRaises(LogisticsCenterMessage.DoesNotExist):
            process_logistics_center_message.apply_async((123,))

    def test_invalid_message(self):
        # the task should raise errors if any occur that cannot be handled so
        # we know it is failing
        with self.assertRaises(KeyError):
            process_logistics_center_message.apply_async(
                (self.logistics_center_message_invalid_1.pk,)
            )

        with self.assertRaises(KeyError):
            process_logistics_center_message.apply_async(
                (self.logistics_center_message_invalid_2.pk,)
            )

    def test_order_id_not_found(self):
        with self.assertRaises(Order.DoesNotExist):
            process_logistics_center_message.apply_async(
                (self.logistics_center_message_non_existing_order.pk,)
            )

        # no order status records should have been created
        self.assertEquals(len(LogisticsCenterOrderStatus.objects.all()), 0)

        # logistics center status should not have been set
        self.assertEquals(Order.objects.first().logistics_center_status, None)

    def test_order_successful_status_updates(self):
        process_logistics_center_message.apply_async(
            (self.logistics_center_message_order_picked_status_update.pk,)
        )

        # an order status record was created
        self.assertEquals(len(LogisticsCenterOrderStatus.objects.all()), 1)
        order_status = LogisticsCenterOrderStatus.objects.first()
        self.assertEquals(order_status.status, 'PICKED')
        self.assertEquals(
            order_status.status_date_time.strftime('%Y-%m-%d %H:%M:%S'),
            '2024-08-01 12:00:00',
        )

        # logistics center status should have been set
        self.order.refresh_from_db()
        self.assertEquals(self.order.logistics_center_status, 'PICKED')

        process_logistics_center_message.apply_async(
            (self.logistics_center_message_order_transported_status_update.pk,)
        )

        # another order status record was created
        self.assertEquals(len(LogisticsCenterOrderStatus.objects.all()), 2)
        order_status = LogisticsCenterOrderStatus.objects.all()[1]
        self.assertEquals(order_status.status, 'TRANSPORTED')
        self.assertEquals(
            order_status.status_date_time.strftime('%Y-%m-%d %H:%M:%S'),
            '2024-08-02 12:00:00',
        )

        # logistics center status should have been updated since the received
        # status date is newer than the previous one
        self.order.refresh_from_db()
        self.assertEquals(self.order.logistics_center_status, 'TRANSPORTED')

        process_logistics_center_message.apply_async(
            (self.logistics_center_message_order_received_late_status_update.pk,)
        )

        # another order status record was created
        self.assertEquals(len(LogisticsCenterOrderStatus.objects.all()), 3)
        order_status = LogisticsCenterOrderStatus.objects.all()[2]
        self.assertEquals(order_status.status, 'RECEIVED')
        self.assertEquals(
            order_status.status_date_time.strftime('%Y-%m-%d %H:%M:%S'),
            '2024-08-01 06:00:00',
        )

        # logistics center status should not have been updated since the
        # received status date is older than the previous one
        self.order.refresh_from_db()
        self.assertEquals(self.order.logistics_center_status, 'TRANSPORTED')

        process_logistics_center_message.apply_async(
            (self.logistics_center_message_order_transported_status_update.pk,)
        )

        # receiving a status update again should not create another order
        # status record
        self.assertEquals(len(LogisticsCenterOrderStatus.objects.all()), 3)

    def test_order_successful_status_updates_deprecated_id(self):
        process_logistics_center_message.apply_async(
            (self.logistics_center_message_order_picked_status_update_deprecated_id.pk,)
        )

        # an order status record was created
        self.assertEquals(len(LogisticsCenterOrderStatus.objects.all()), 1)
        order_status = LogisticsCenterOrderStatus.objects.first()
        self.assertEquals(order_status.status, 'PICKED')
        self.assertEquals(
            order_status.status_date_time.strftime('%Y-%m-%d %H:%M:%S'),
            '2024-08-01 12:00:00',
        )

        # logistics center status should have been set
        self.order.refresh_from_db()
        self.assertEquals(self.order.logistics_center_status, 'PICKED')


class ProcessLogisticsCenterShipOrderMessageTestCase(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            name='supplier name',
        )
        self.brand = Brand.objects.create(
            name='brand name',
        )
        self.product_1 = Product.objects.create(
            brand=self.brand,
            supplier=self.supplier,
            name='product 1 name',
            sku='1',
            cost_price=50,
            sale_price=60,
        )
        self.product_2 = Product.objects.create(
            brand=self.brand,
            supplier=self.supplier,
            name='product 2 name',
            sku='2',
            cost_price=70,
            sale_price=80,
        )

        # create the campaign infrastructure for the orders we need
        organization = Organization.objects.create(
            name='Test organization',
            manager_full_name='Test manager',
            manager_phone_number='0500000009',
            manager_email='manager@test.test',
        )
        campaign = Campaign.objects.create(
            name='Test campaign',
            organization=organization,
            status=Campaign.CampaignStatusEnum.ACTIVE.name,
            start_date_time=datetime.now(),
            end_date_time=datetime.now(),
        )
        employee_group = EmployeeGroup.objects.create(
            name='Test employee group 1',
            organization=organization,
            delivery_city='Office1',
            delivery_street='Office street 1',
            delivery_street_number='1',
            delivery_apartment_number='2',
            delivery_location=DeliveryLocationEnum.ToHome.name,
        )
        employee_group_campaign = EmployeeGroupCampaign.objects.create(
            employee_group=employee_group, campaign=campaign, budget_per_employee=100
        )
        employee = Employee.objects.create(
            employee_group=employee_group,
            first_name='Test',
            last_name='Employee 1',
            email='test1@test.test',
        )
        employee_group_campaign_product = EmployeeGroupCampaignProduct.objects.create(
            employee_group_campaign_id=employee_group_campaign,
            product_id=self.product_1,
        )

        # the order we need for testing outbound
        self.order = Order.objects.create(
            campaign_employee_id=CampaignEmployee.objects.get(
                campaign=campaign, employee=employee
            ),
            order_date_time=datetime.now(),
            cost_from_budget=100,
            cost_added=0,
            status=Order.OrderStatusEnum.PENDING.name,
            full_name='Test name 1',
            phone_number='0500000000',
            additional_phone_number='050000001',
            delivery_city='City1',
            delivery_street='Main1',
            delivery_street_number='1',
            delivery_apartment_number='1',
            delivery_additional_details='Additional 1',
        )
        OrderProduct.objects.create(
            order_id=self.order,
            product_id=employee_group_campaign_product,
            quantity=1,
        )

        self.logistics_center_message_invalid_1 = LogisticsCenterMessage.objects.create(
            center=LogisticsCenterEnum.ORIAN.name,
            message_type=LogisticsCenterMessageTypeEnum.SHIP_ORDER.name,
            raw_body='{}',
        )
        self.logistics_center_message_invalid_2 = LogisticsCenterMessage.objects.create(
            center=LogisticsCenterEnum.ORIAN.name,
            message_type=LogisticsCenterMessageTypeEnum.SHIP_ORDER.name,
            raw_body=json.dumps({'DATACOLLECTION': {'DATA': {}}}),
        )
        self.logistics_center_message_non_existing_order = (
            LogisticsCenterMessage.objects.create(
                center=LogisticsCenterEnum.ORIAN.name,
                message_type=LogisticsCenterMessageTypeEnum.SHIP_ORDER.name,
                raw_body=json.dumps(
                    {
                        'DATACOLLECTION': {
                            'DATA': {
                                'CONSIGNEE': 'NKS',
                                'ORDERID': 'unknown',
                                'ORDERTYPE': 'CUSTOMER',
                                'TARGETCOMPANY': _platform_id_to_orian_id(-999),
                                'COMPANYTYPE': 'CUSTOMER',
                                'STATUS': 'SHIPPED',
                                'SHIPPEDDATE': '8/1/2024 12:00:00 PM',
                            }
                        }
                    }
                ),
            )
        )
        self.logistics_center_message_ship_order = (
            LogisticsCenterMessage.objects.create(
                center=LogisticsCenterEnum.ORIAN.name,
                message_type=LogisticsCenterMessageTypeEnum.SHIP_ORDER.name,
                raw_body=json.dumps(
                    {
                        'DATACOLLECTION': {
                            'DATA': {
                                'CONSIGNEE': 'NKS',
                                'ORDERID': Order.objects.get(pk=self.order.pk).order_id,
                                'ORDERTYPE': 'CUSTOMER',
                                'TARGETCOMPANY': _platform_id_to_orian_id(-999),
                                'COMPANYTYPE': 'CUSTOMER',
                                'STATUS': 'SHIPPED',
                                'SHIPPEDDATE': '8/1/2024 12:00:00 PM',
                            }
                        }
                    }
                ),
            )
        )
        self.logistics_center_message_ship_order_late = (
            LogisticsCenterMessage.objects.create(
                center=LogisticsCenterEnum.ORIAN.name,
                message_type=LogisticsCenterMessageTypeEnum.SHIP_ORDER.name,
                raw_body=json.dumps(
                    {
                        'DATACOLLECTION': {
                            'DATA': {
                                'CONSIGNEE': 'NKS',
                                'ORDERID': Order.objects.get(pk=self.order.pk).order_id,
                                'ORDERTYPE': 'CUSTOMER',
                                'TARGETCOMPANY': _platform_id_to_orian_id(-999),
                                'COMPANYTYPE': 'CUSTOMER',
                                'STATUS': 'SHIPPEDAGAIN',
                                'SHIPPEDDATE': '8/1/2024 06:00:00 AM',
                            }
                        }
                    }
                ),
            )
        )
        self.logistics_center_message_ship_order_deprecated_id = (
            LogisticsCenterMessage.objects.create(
                center=LogisticsCenterEnum.ORIAN.name,
                message_type=LogisticsCenterMessageTypeEnum.SHIP_ORDER.name,
                raw_body=json.dumps(
                    {
                        'DATACOLLECTION': {
                            'DATA': {
                                'CONSIGNEE': 'NKS',
                                'ORDERID': _platform_id_to_orian_id(self.order.pk),
                                'ORDERTYPE': 'CUSTOMER',
                                'TARGETCOMPANY': _platform_id_to_orian_id(-999),
                                'COMPANYTYPE': 'CUSTOMER',
                                'STATUS': 'SHIPPED',
                                'SHIPPEDDATE': '8/1/2024 12:00:00 PM',
                            }
                        }
                    }
                ),
            )
        )

    def test_message_not_found(self):
        # the task should raise errors if any occur that cannot be handled so
        # we know it is failing
        with self.assertRaises(LogisticsCenterMessage.DoesNotExist):
            process_logistics_center_message.apply_async((123,))

    def test_invalid_message(self):
        # the task should raise errors if any occur that cannot be handled so
        # we know it is failing
        with self.assertRaises(KeyError):
            process_logistics_center_message.apply_async(
                (self.logistics_center_message_invalid_1.pk,)
            )

        with self.assertRaises(KeyError):
            process_logistics_center_message.apply_async(
                (self.logistics_center_message_invalid_2.pk,)
            )

    def test_order_id_not_found(self):
        with self.assertRaises(Order.DoesNotExist):
            process_logistics_center_message.apply_async(
                (self.logistics_center_message_non_existing_order.pk,)
            )

        # no order status records should have been created
        self.assertEquals(len(LogisticsCenterOrderStatus.objects.all()), 0)

        # logistics center status should not have been set
        self.assertEquals(Order.objects.first().logistics_center_status, None)

    def test_order_successful_status_updates(self):
        process_logistics_center_message.apply_async(
            (self.logistics_center_message_ship_order.pk,)
        )

        # an order status record was created
        self.assertEquals(len(LogisticsCenterOrderStatus.objects.all()), 1)
        order_status = LogisticsCenterOrderStatus.objects.first()
        self.assertEquals(order_status.status, 'SHIPPED')
        self.assertEquals(
            order_status.status_date_time.strftime('%Y-%m-%d %H:%M:%S'),
            '2024-08-01 12:00:00',
        )

        # logistics center status should have been set
        self.order.refresh_from_db()
        self.assertEquals(self.order.logistics_center_status, 'SHIPPED')

        process_logistics_center_message.apply_async(
            (self.logistics_center_message_ship_order_late.pk,)
        )

        # another order status record was created
        self.assertEquals(len(LogisticsCenterOrderStatus.objects.all()), 2)
        order_status = LogisticsCenterOrderStatus.objects.all()[1]
        self.assertEquals(order_status.status, 'SHIPPEDAGAIN')
        self.assertEquals(
            order_status.status_date_time.strftime('%Y-%m-%d %H:%M:%S'),
            '2024-08-01 06:00:00',
        )

        # logistics center status should not have been updated since the
        # received status date is older than the previous one
        self.order.refresh_from_db()
        self.assertEquals(self.order.logistics_center_status, 'SHIPPED')

        process_logistics_center_message.apply_async(
            (self.logistics_center_message_ship_order.pk,)
        )

        # receiving a ship order again should not create another order
        # status record
        self.assertEquals(len(LogisticsCenterOrderStatus.objects.all()), 2)

    def test_order_successful_status_updates_deprecated_id(self):
        process_logistics_center_message.apply_async(
            (self.logistics_center_message_ship_order_deprecated_id.pk,)
        )

        # an order status record was created
        self.assertEquals(len(LogisticsCenterOrderStatus.objects.all()), 1)
        order_status = LogisticsCenterOrderStatus.objects.first()
        self.assertEquals(order_status.status, 'SHIPPED')
        self.assertEquals(
            order_status.status_date_time.strftime('%Y-%m-%d %H:%M:%S'),
            '2024-08-01 12:00:00',
        )

        # logistics center status should have been set
        self.order.refresh_from_db()
        self.assertEquals(self.order.logistics_center_status, 'SHIPPED')