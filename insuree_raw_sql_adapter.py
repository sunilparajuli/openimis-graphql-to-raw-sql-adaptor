class OrderedSet:
    def __init__(self):
        self.items = {}
    def add(self, item):
        self.items[item] = None
    def __iter__(self):
        return iter(self.items.keys())
    def __bool__(self):
        return bool(self.items)

class RawInsureeAdapter:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.params = []
        self.joins = OrderedSet()
        self.where_clauses = []
        self.order_by = []
        self._build_query()

    def _build_query(self):
        self.base_table = 'tblInsuree'
        self.table_alias = 'c'
        
        # We always filter out soft-deleted
        self.where_clauses.append(f'{self.table_alias}.ValidityTo IS NULL')

        import re
        normalized_kwargs = {}
        for key, value in self.kwargs.items():
            if key in ['show_history', 'ignore_location', 'client_mutation_id', 'parentLocation', 'parentLocationLevel', 'parent_location', 'parent_location_level', 'orderBy', 'first', 'last', 'before', 'after', 'offset']:
                normalized_kwargs[key] = value
                continue
            
            snake_key = re.sub(r'(?<!^)(?=[A-Z])', '_', key).lower()
            
            suffix = ""
            for s in ['__gt', '__gte', '__lt', '__lte', '__icontains', '__istartswith', '__isnull']:
                if snake_key.endswith(s):
                    suffix = s
                    snake_key = snake_key[:-len(s)]
                    break
                    
            snake_key = re.sub(r'_+', '_', snake_key)
            
            # Insuree specific normalizations
            snake_key = snake_key.replace('family_confirmation_type_code', 'family__confirmation_type__code')
            snake_key = snake_key.replace('gender_code', 'gender__code')
            if snake_key.startswith('family_') and not snake_key.startswith('family__'):
                snake_key = snake_key.replace('family_', 'family__', 1)
            if snake_key.startswith('current_village_') and not snake_key.startswith('current_village__'):
                snake_key = snake_key.replace('current_village_', 'current_village__', 1)
                
            snake_key += suffix
            normalized_kwargs[snake_key] = value

        kwargs = normalized_kwargs
        
        # 1. Special Filters
        show_history = kwargs.get('show_history', False)
        # validity_to__isnull=True is added by default in Django if not show_history. We already added it above.
        
        parent_location = kwargs.get('parentLocation') or kwargs.get('parent_location')
        if parent_location is not None:
            parent_location_level = kwargs.get('parentLocationLevel') or kwargs.get('parent_location_level')
            if parent_location_level is not None:
                from location.apps import LocationConfig
                depth = len(LocationConfig.location_types) - parent_location_level - 1
                
                self.joins.add('LEFT JOIN tblLocations v0 WITH (NOLOCK) ON c.CurrentVillage = v0.LocationId AND v0.ValidityTo IS NULL')
                self.joins.add('LEFT JOIN tblFamilies f WITH (NOLOCK) ON c.FamilyID = f.FamilyID AND f.ValidityTo IS NULL')
                self.joins.add('LEFT JOIN tblLocations fl0 WITH (NOLOCK) ON f.LocationId = fl0.LocationId AND fl0.ValidityTo IS NULL')
                
                v_alias = 'v0'
                fl_alias = 'fl0'
                
                for i in range(depth):
                    next_v = f'v{i+1}'
                    next_fl = f'fl{i+1}'
                    self.joins.add(f'LEFT JOIN tblLocations {next_v} WITH (NOLOCK) ON {v_alias}.ParentLocationId = {next_v}.LocationId AND {next_v}.ValidityTo IS NULL')
                    self.joins.add(f'LEFT JOIN tblLocations {next_fl} WITH (NOLOCK) ON {fl_alias}.ParentLocationId = {next_fl}.LocationId AND {next_fl}.ValidityTo IS NULL')
                    v_alias = next_v
                    fl_alias = next_fl
                    
                self.where_clauses.append(f'(({v_alias}.LocationUUID IS NOT NULL AND {v_alias}.LocationUUID = %s) OR (c.CurrentVillage IS NULL AND {fl_alias}.LocationUUID = %s))')
                self.params.extend([parent_location, parent_location])

        # 2. Base Joins based on prefixes
        if any(k.startswith('family__') for k in kwargs) or parent_location is not None:
            self.joins.add('LEFT JOIN tblFamilies f WITH (NOLOCK) ON c.FamilyID = f.FamilyID AND f.ValidityTo IS NULL')
        
        # Note: family__confirmation_type__code just filters on f.ConfirmationType, no join to tblConfirmationTypes needed
        # Note: gender__code just filters on c.Gender, no join to tblGender needed

        prefix_alias_map = {
            'family__': 'f',
            'current_village__': 'v0',
        }
        
        column_map = {
            'uuid': 'InsureeUUID', 'chf_id': 'CHFID', 'chfid': 'CHFID',
            'last_name': 'LastName', 'other_names': 'OtherNames',
            'nin': 'NIN', 'passport': 'passport', 'phone': 'Phone',
            'gender__code': 'Gender', 'gender': 'Gender',
            'marital': 'Marital', 'dob': 'DOB',
            'validity_from': 'ValidityFrom', 'validityFrom': 'ValidityFrom',
            'status': 'InsureeStatus'
        }

        alias_column_overrides = {
            'f': {'uuid': 'FamilyUUID', 'confirmation_type__code': 'ConfirmationType', 'location__uuid': 'LocationUUID'},
            'v0': {'uuid': 'LocationUUID'}
        }

        for key, value in kwargs.items():
            if key in ['show_history', 'ignore_location', 'client_mutation_id', 'parentLocation', 'parentLocationLevel', 'parent_location', 'parent_location_level', 'orderBy', 'first', 'last', 'before', 'after', 'offset']:
                continue
            
            if isinstance(value, str):
                value = value.strip()
            
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
                # OPTIMIZATION: Converted to starts-with to avoid full table scans
                operator, base_key = 'LIKE', base_key[:-11]
                value = f'{value}%'
            elif base_key.endswith('__istartswith'):
                operator, base_key = 'LIKE', base_key[:-13]
                value = f'{value}%'
            elif base_key.endswith('__isnull'):
                is_null_check = True
                operator = 'IS NULL' if value else 'IS NOT NULL'
                base_key = base_key[:-8]
            
            col_prefix = f'{self.table_alias}.'
            real_key = base_key
            matched_alias = None
            
            for prefix, join_alias in prefix_alias_map.items():
                if base_key.startswith(prefix):
                    col_prefix = f'{join_alias}.'
                    matched_alias = join_alias
                    real_key = base_key[len(prefix):]
                    break
            
            col_name = None
            if matched_alias and real_key in alias_column_overrides.get(matched_alias, {}):
                col_name = alias_column_overrides[matched_alias][real_key]
            elif base_key == 'family':
                # Map 'family' (after removing __isnull) to FamilyID
                col_prefix = f'{self.table_alias}.'
                col_name = 'FamilyID'
            else:
                col_name = column_map.get(real_key, real_key)
            
            if is_null_check:
                self.where_clauses.append(f'{col_prefix}{col_name} {operator}')
            else:
                self.where_clauses.append(f'{col_prefix}{col_name} {operator} %s')
                self.params.append(value)

    def get_id_query(self):
        query = f'SELECT {self.table_alias}.InsureeID FROM {self.base_table} {self.table_alias} WITH (NOLOCK) '
        if self.joins:
            query += ' '.join(self.joins) + ' '
        
        if self.where_clauses:
            query += 'WHERE ' + ' AND '.join(self.where_clauses) + ' '
            
        return query, self.params
