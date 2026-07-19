import base64
import re

class OrderedSet:
    def __init__(self):
        self.items = {}
    def add(self, item):
        self.items[item] = None
    def __iter__(self):
        return iter(self.items.keys())
    def __bool__(self):
        return bool(self.items)

class RawPolicyAdapter:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.params = []
        self.joins = OrderedSet()
        self.where_clauses = []
        self._build_query()

    def decode_relay_id(self, relay_id_str):
        try:
            decoded = base64.b64decode(relay_id_str).decode('utf-8')
            return int(decoded.split(':')[1])
        except Exception:
            return relay_id_str

    def _build_query(self):
        self.base_table = 'tblPolicy'
        self.table_alias = 'p'
        
        show_history = self.kwargs.get('showHistory', False)
        if not show_history:
            self.where_clauses.append(f'{self.table_alias}.ValidityTo IS NULL')

        normalized_kwargs = {}
        for key, value in self.kwargs.items():
            if key in ['showHistory', 'showInactive', 'client_mutation_id', 'orderBy', 'first', 'last', 'before', 'after', 'offset', 'districtId', 'regionId', 'district_id', 'region_id', 'confirmationType']:
                normalized_kwargs[key] = value
                continue
                
            if isinstance(value, str):
                value = value.strip()
            
            snake_key = re.sub(r'(?<!^)(?=[A-Z])', '_', key).lower()
            
            suffix = ""
            for s in ['__gt', '__gte', '__lt', '__lte', '__icontains', '__istartswith', '__isnull']:
                if snake_key.endswith(s):
                    suffix = s
                    snake_key = snake_key[:-len(s)]
                    break
                    
            snake_key = re.sub(r'_+', '_', snake_key)
            
            # Mapping insuree_policies__insuree to ip__i
            if snake_key.startswith('insuree_policies_insuree_'):
                snake_key = snake_key.replace('insuree_policies_insuree_', 'ipi__', 1)
            elif snake_key.startswith('insuree_policies_insuree__'):
                snake_key = snake_key.replace('insuree_policies_insuree__', 'ipi__', 1)
            
            if snake_key.endswith('_id'):
                snake_key = snake_key.replace('_id', '__id')

            snake_key += suffix
            normalized_kwargs[snake_key] = value

        kwargs = normalized_kwargs
        
        # Location Hierarchy mapping via Family
        location_id = kwargs.get('districtId') or kwargs.get('district_id') or kwargs.get('regionId') or kwargs.get('region_id')
        if location_id:
            location_level = 2 if (kwargs.get('districtId') or kwargs.get('district_id')) else 1
            
            self.joins.add('LEFT JOIN tblFamilies f WITH (NOLOCK) ON p.FamilyID = f.FamilyID AND f.ValidityTo IS NULL')
            
            from location.apps import LocationConfig
            depth = len(LocationConfig.location_types) - location_level
            
            self.joins.add('LEFT JOIN tblLocations loc0 WITH (NOLOCK) ON f.LocationId = loc0.LocationId AND loc0.ValidityTo IS NULL')
            loc_alias = 'loc0'
            
            for i in range(depth):
                next_loc = f'loc{i+1}'
                self.joins.add(f'LEFT JOIN tblLocations {next_loc} WITH (NOLOCK) ON {loc_alias}.ParentLocationId = {next_loc}.LocationId AND {next_loc}.ValidityTo IS NULL')
                loc_alias = next_loc
                
            self.where_clauses.append(f'{loc_alias}.LocationId = %s')
            self.params.append(location_id)

        # confirmationType mapping via Family
        confirmation_type = kwargs.get('confirmationType')
        if confirmation_type:
            self.joins.add('LEFT JOIN tblFamilies f WITH (NOLOCK) ON p.FamilyID = f.FamilyID AND f.ValidityTo IS NULL')
            self.where_clauses.append('f.ConfirmationType = %s')
            self.params.append(confirmation_type)
            
        # Joins for insuree_policies__insuree (ipi__)
        if any(k.startswith('ipi__') for k in kwargs):
            self.joins.add('LEFT JOIN tblInsureePolicy ip WITH (NOLOCK) ON p.PolicyID = ip.PolicyID AND ip.ValidityTo IS NULL')
            self.joins.add('LEFT JOIN tblInsuree i WITH (NOLOCK) ON ip.InsureeID = i.InsureeID AND i.ValidityTo IS NULL')

        prefix_alias_map = {
            'ipi__': 'i',
        }
        
        column_map = {
            'uuid': 'PolicyUUID', 'enroll_date': 'EnrollDate', 'start_date': 'StartDate',
            'effective_date': 'EffectiveDate', 'expiry_date': 'ExpiryDate',
            'value': 'PolicyValue', 'status': 'PolicyStatus', 'stage': 'PolicyStage',
            'product__id': 'ProdID', 'officer__id': 'OfficerID',
            'other_names': 'OtherNames', 'last_name': 'LastName', 'chf_id': 'CHFID',
            'chfid': 'CHFID', 'chf__id': 'CHFID'
        }

        for key, value in kwargs.items():
            if key in ['showHistory', 'showInactive', 'client_mutation_id', 'orderBy', 'first', 'last', 'before', 'after', 'offset', 'districtId', 'regionId', 'district_id', 'region_id', 'confirmationType']:
                continue
            
            operator = '='
            is_null_check = False
            base_key = key
            
            if base_key.endswith('__gt'):
                operator, base_key = '>', base_key[:-4]
            elif base_key.endswith('__gte'):
                operator, base_key = '>=', base_key[:-5]
            elif base_key.endswith('__lt'):
                operator, base_key = '<', base_key[:-4]
            elif base_key.endswith('__lte'):
                operator, base_key = '<=', base_key[:-5]
            elif base_key.endswith('__icontains'):
                operator, base_key = 'LIKE', base_key[:-11]
                value = f'{value}%'
            elif base_key.endswith('__istartswith'):
                operator, base_key = 'LIKE', base_key[:-13]
                value = f'{value}%'
            elif base_key.endswith('__isnull'):
                is_null_check = True
                operator = 'IS NULL' if value else 'IS NOT NULL'
                base_key = base_key[:-8]
            
            # Decode Relay ID if needed
            if base_key.endswith('__id') and isinstance(value, str):
                value = self.decode_relay_id(value)
            
            col_prefix = f'{self.table_alias}.'
            real_key = base_key
            matched_alias = None
            
            for prefix, join_alias in prefix_alias_map.items():
                if base_key.startswith(prefix):
                    col_prefix = f'{join_alias}.'
                    matched_alias = join_alias
                    real_key = base_key[len(prefix):]
                    break
            
            col_name = column_map.get(real_key, real_key)
            
            if is_null_check:
                self.where_clauses.append(f'{col_prefix}{col_name} {operator}')
            else:
                self.where_clauses.append(f'{col_prefix}{col_name} {operator} %s')
                self.params.append(value)

    def get_id_query(self):
        query = f'SELECT DISTINCT {self.table_alias}.PolicyID FROM {self.base_table} {self.table_alias} WITH (NOLOCK) '
        if self.joins:
            query += ' '.join(self.joins) + ' '
        
        if self.where_clauses:
            query += 'WHERE ' + ' AND '.join(self.where_clauses) + ' '
            
        return query, self.params
