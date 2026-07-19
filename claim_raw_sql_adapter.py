import datetime
import json
from django.db import connection

class OrderedSet:
    def __init__(self):
        self.items = {}
    def add(self, item):
        self.items[item] = None
    def __iter__(self):
        return iter(self.items.keys())
    def __bool__(self):
        return bool(self.items)

class RawClaimAdapter:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.params = []
        self.joins = OrderedSet()
        self.where_clauses = []
        self.order_by = []
        self._build_query()

    def _build_query(self):
        # Base table
        self.base_table = 'tblClaim'
        self.table_alias = 'c'
        
        # We always filter out soft-deleted (ValidityTo is NULL)
        self.where_clauses.append(f'{self.table_alias}.ValidityTo IS NULL')

        import re
        normalized_kwargs = {}
        for key, value in self.kwargs.items():
            if key in ['items', 'services', 'attachment_status', 'care_type', 'show_restored', 'code_is_not', 'json_ext', 'diagnosisVariance', 'orderBy', 'first', 'last', 'before', 'after', 'offset']:
                normalized_kwargs[key] = value
                continue
            
            # 1. Convert camelCase to snake_case
            snake_key = re.sub(r'(?<!^)(?=[A-Z])', '_', key).lower()
            
            # 2. Preserve known suffixes by temporarily hiding them
            suffix = ""
            for s in ['__gt', '__gte', '__lt', '__lte', '__icontains', '__istartswith']:
                if snake_key.endswith(s):
                    suffix = s
                    snake_key = snake_key[:-len(s)]
                    break
                    
            # 3. Reduce all multiple underscores to a single underscore
            snake_key = re.sub(r'_+', '_', snake_key)
            
            # 4. Explicitly rebuild the exact double-underscore prefixes we expect
            snake_key = snake_key.replace('health_facility_location_parent_', 'health_facility__location__parent__')
            snake_key = snake_key.replace('health_facility_location_', 'health_facility__location__')
            if snake_key.startswith('health_facility_') and not snake_key.startswith('health_facility__'):
                snake_key = snake_key.replace('health_facility_', 'health_facility__', 1)
            if snake_key.startswith('icd_'):
                snake_key = snake_key.replace('icd_', 'icd__', 1)
            if snake_key.startswith('admin_'):
                snake_key = snake_key.replace('admin_', 'admin__', 1)
            if snake_key.startswith('insuree_'):
                snake_key = snake_key.replace('insuree_', 'insuree__', 1)
                
            # 5. Add suffix back
            snake_key += suffix
            normalized_kwargs[snake_key] = value

        kwargs = normalized_kwargs
        
        # 1. Special Filters
        show_restored = kwargs.get('show_restored', None)
        if show_restored:
            self.where_clauses.append(f'{self.table_alias}.RestoreID IS NOT NULL')

        items = kwargs.get('items', None)
        if items:
            self.joins.add('INNER JOIN tblClaimItem ci WITH (NOLOCK) ON c.ClaimID = ci.ClaimID AND ci.ValidityTo IS NULL')
            self.joins.add('INNER JOIN tblItem i WITH (NOLOCK) ON ci.ItemID = i.ItemID AND i.ValidityTo IS NULL')
            self._add_in_clause('i.ItemCode', items)

        services = kwargs.get('services', None)
        if services:
            self.joins.add('INNER JOIN tblClaimService cs WITH (NOLOCK) ON c.ClaimID = cs.ClaimID AND cs.ValidityTo IS NULL')
            self.joins.add('INNER JOIN tblService s WITH (NOLOCK) ON cs.ServiceID = s.ServiceID AND s.ValidityTo IS NULL')
            self._add_in_clause('s.ServiceCode', services)

        attachment_status = kwargs.get('attachment_status', 0)
        if attachment_status == 1:
            self.where_clauses.append(f'{self.table_alias}.Attachment = 1')
        elif attachment_status == 2:
            self.where_clauses.append(f'({self.table_alias}.Attachment = 0 OR {self.table_alias}.Attachment IS NULL)')

        care_type = kwargs.get('care_type', None)
        if care_type:
            self.where_clauses.append(f'{self.table_alias}.CareType = %s')
            self.params.append(care_type)

        json_ext = kwargs.get('json_ext', None)
        if json_ext:
            self.where_clauses.append(f'{self.table_alias}.JsonExt LIKE %s')
            self.params.append(f'%{json_ext}%')
            
        code_is_not = kwargs.get('code_is_not', None)
        if code_is_not:
            self.where_clauses.append(f'{self.table_alias}.ClaimCode != %s')
            self.params.append(code_is_not)
            
        # 2. Joins based on prefixes
        if any(k.startswith('health_facility__') for k in kwargs):
            self.joins.add('LEFT JOIN tblHF hf WITH (NOLOCK) ON c.HFID = hf.HfID AND hf.ValidityTo IS NULL')
        if any(k.startswith('health_facility__location__') for k in kwargs):
            self.joins.add('LEFT JOIN tblLocations loc WITH (NOLOCK) ON hf.LocationId = loc.LocationId AND loc.ValidityTo IS NULL')
        if any(k.startswith('health_facility__location__parent__') for k in kwargs):
            self.joins.add('LEFT JOIN tblLocations loc_parent WITH (NOLOCK) ON loc.ParentLocationId = loc_parent.LocationId AND loc_parent.ValidityTo IS NULL')
        if any(k.startswith('icd__') for k in kwargs):
            self.joins.add('LEFT JOIN tblICDCodes d WITH (NOLOCK) ON c.ICDID = d.ICDID AND d.ValidityTo IS NULL')
        if any(k.startswith('admin__') for k in kwargs):
            self.joins.add('LEFT JOIN tblClaimAdmin a WITH (NOLOCK) ON c.ClaimAdminId = a.ClaimAdminId AND a.ValidityTo IS NULL')
        if any(k.startswith('insuree__') for k in kwargs):
            self.joins.add('LEFT JOIN tblInsuree ins WITH (NOLOCK) ON c.InsureeID = ins.InsureeID AND ins.ValidityTo IS NULL')

        # Prefix to alias mapping (longest first)
        prefix_alias_map = {
            'health_facility__location__parent__': 'loc_parent',
            'health_facility__location__': 'loc',
            'health_facility__': 'hf',
            'icd__': 'd',
            'admin__': 'a',
            'insuree__': 'ins',
        }
        
        column_map = {
            'uuid': 'ClaimUUID', 'code': 'ClaimCode', 'status': 'ClaimStatus',
            'date_claimed': 'DateClaimed', 'dateClaimed': 'DateClaimed',
            'date_from': 'DateFrom', 'dateFrom': 'DateFrom',
            'date_to': 'DateTo', 'dateTo': 'DateTo',
            'date_processed': 'DateProcessed', 'dateProcessed': 'DateProcessed',
            'feedback_status': 'FeedbackStatus', 'feedbackStatus': 'FeedbackStatus',
            'review_status': 'ReviewStatus', 'reviewStatus': 'ReviewStatus',
            'claimed': 'Claimed', 'approved': 'Approved',
            'visit_type': 'VisitType', 'visitType': 'VisitType',
            'pre_authorization': 'GuaranteeId', 'preAuthorization': 'GuaranteeId'
        }

        # Override column mappings for specific aliases
        alias_column_overrides = {
            'hf': {'uuid': 'HfUUID', 'code': 'HFCode', 'name': 'HFName'},
            'loc': {'uuid': 'LocationUUID', 'code': 'LocationCode', 'name': 'LocationName'},
            'loc_parent': {'uuid': 'LocationUUID', 'code': 'LocationCode', 'name': 'LocationName'},
            'ins': {'uuid': 'InsureeUUID', 'chf_id': 'CHFID', 'lastName': 'LastName'},
            'd': {'id': 'ICDID', 'code': 'ICDCode'},
            'a': {'code': 'ClaimAdminCode', 'uuid': 'ClaimAdminUUID'}
        }

        def decode_relay_id(relay_val):
            import base64
            try:
                decoded = base64.b64decode(relay_val).decode('utf-8')
                if ':' in decoded:
                    return decoded.split(':')[1]
            except Exception:
                pass
            return relay_val

        for key, value in kwargs.items():
            if key in ['items', 'services', 'attachment_status', 'care_type', 'show_restored', 'code_is_not', 'json_ext', 'diagnosisVariance', 'orderBy', 'first', 'last', 'before', 'after', 'offset']:
                continue
            
            if isinstance(value, str):
                value = value.strip()
                if key.endswith('_Id') or key.endswith('_id') or key == 'id':
                    value = decode_relay_id(value)

            col_prefix = f'{self.table_alias}.'
            real_key = key
            matched_alias = None
            
            for prefix, join_alias in prefix_alias_map.items():
                if key.startswith(prefix):
                    col_prefix = f'{join_alias}.'
                    matched_alias = join_alias
                    real_key = key[len(prefix):]
                    break
            
            # Determine Operator
            operator = '='
            if real_key.endswith('__gt'):
                operator, real_key = '>', real_key[:-4]
            elif real_key.endswith('__gte'):
                operator, real_key = '>=', real_key[:-5]
            elif real_key.endswith('__lt'):
                operator, real_key = '<', real_key[:-4]
            elif real_key.endswith('__lte'):
                operator, real_key = '<=', real_key[:-5]
            elif real_key.endswith('__icontains'):
                # OPTIMIZATION: Converted to starts-with to avoid full table scans
                operator, real_key = 'LIKE', real_key[:-11]
                value = f'{value}%'
            elif real_key.endswith('__istartswith'):
                operator, real_key = 'LIKE', real_key[:-13]
                value = f'{value}%'
            
            # Map column name
            col_name = None
            if matched_alias and real_key in alias_column_overrides.get(matched_alias, {}):
                col_name = alias_column_overrides[matched_alias][real_key]
            else:
                col_name = column_map.get(real_key, real_key)
            
            self.where_clauses.append(f'{col_prefix}{col_name} {operator} %s')
            self.params.append(value)
            
        # 3. Order By
        order_by_list = kwargs.get('orderBy', [])
        for ob in order_by_list:
            desc = 'DESC' if ob.startswith('-') else 'ASC'
            ob_clean = ob.lstrip('-')
            db_col = column_map.get(ob_clean, ob_clean)
            self.order_by.append(f'{self.table_alias}.{db_col} {desc}')

    def _add_in_clause(self, col, values):
        placeholders = ', '.join(['%s'] * len(values))
        self.where_clauses.append(f'{col} IN ({placeholders})')
        self.params.extend(values)

    def get_id_query(self):
        query = f'SELECT {self.table_alias}.ClaimID FROM {self.base_table} {self.table_alias} WITH (NOLOCK) '
        if self.joins:
            query += ' '.join(self.joins) + ' '
        
        if self.where_clauses:
            query += 'WHERE ' + ' AND '.join(self.where_clauses) + ' '
            
        return query, self.params

    def execute(self):
        query = f'SELECT {self.table_alias}.* FROM {self.base_table} {self.table_alias} WITH (NOLOCK) '
        if self.joins:
            query += ' '.join(self.joins) + ' '
        
        if self.where_clauses:
            query += 'WHERE ' + ' AND '.join(self.where_clauses) + ' '
            
        if self.order_by:
            query += 'ORDER BY ' + ', '.join(self.order_by)
            
        with connection.cursor() as cursor:
            cursor.execute(query, self.params)
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

